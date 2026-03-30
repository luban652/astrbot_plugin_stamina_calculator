import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

@register("stamina_calculator", "AstrBot-Assistant", "游戏体力回复计算助手。支持设置25或50体力上限，每30分钟回复1点，支持计算回满时间。", "1.0.0")
class StaminaCalculatorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "stamina_user_data.json"
        
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.user_data = self._load_data_sync()

    def _load_data_sync(self) -> dict:
        if self.db_path.exists():
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[Stamina] 加载数据失败: {e}")
        return {}

    async def _save_data(self):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self.executor, self._save_data_sync)
        except Exception as e:
            logger.error(f"[Stamina] 异步保存数据失败: {e}")

    def _save_data_sync(self):
        try:
            tmp_path = str(self.db_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, str(self.db_path))
        except Exception as e:
            logger.error(f"[Stamina] 物理写入数据失败: {e}")

    async def _get_user_info(self, user_id: str) -> dict:
        if user_id not in self.user_data:
            self.user_data[user_id] = {
                "max_stamina": self.config.get("default_max_stamina", 25),
                "full_time": 0
            }
        return self.user_data[user_id]

    @filter.command("上限")
    async def set_limit(self, event: AstrMessageEvent, limit: int):
        user_id = event.get_sender_id()
        if limit not in [25, 50]:
            yield event.plain_result("错误：目前仅支持设置上限为 25 或 50。")
            return
            
        info = await self._get_user_info(user_id)
        info["max_stamina"] = limit
        await self._save_data()
        yield event.plain_result(f"已将你的体力上限设置为: {limit}")

    @filter.command("火")
    async def calculate(self, event: AstrMessageEvent, current: str = None, remaining: str = None):
        user_id = event.get_sender_id()
        info = await self._get_user_info(user_id)
        
        if current == "当前":
            full_time = info.get("full_time", 0)
            now_ts = time.time()
            if full_time == 0 or full_time < now_ts:
                yield event.plain_result("当前没有记录中的体力恢复任务，或体力已经回满。")
                return
            
            remaining_sec = full_time - now_ts
            rem_h = int(remaining_sec // 3600)
            rem_m = int((remaining_sec % 3600) // 60)
            target_str = datetime.fromtimestamp(full_time).strftime('%Y-%m-%d %H:%M:%S')
            
            yield event.plain_result(
                f"【当前状态】\n"
                f"上限：{info['max_stamina']}\n"
                f"预计回满时间：{target_str}\n"
                f"剩余时间：{rem_h}小时{rem_m}分钟"
            )
            return

        if current is None or remaining is None:
            yield event.plain_result(
                "格式错误。请输入：\n"
                "/火 [当前体力] [下点回复剩余分钟]\n"
                "示例：/火 10 15\n"
                "或输入：/火 当前"
            )
            return

        try:
            current_stamina = int(current)
            remaining_mins = int(remaining)
            max_stamina = info["max_stamina"]
            recovery_rate = self.config.get("recovery_interval_minutes", 30)

            if current_stamina >= max_stamina:
                yield event.plain_result(f"你现在的体力({current_stamina})已达到或超过上限({max_stamina})。")
                return

            total_minutes = (max_stamina - current_stamina - 1) * recovery_rate + remaining_mins
            
            if total_minutes < 0:
                yield event.plain_result("计算出的回复时间为负，请检查输入参数。")
                return

            full_datetime = datetime.now() + timedelta(minutes=total_minutes)
            full_timestamp = full_datetime.timestamp()
            
            info["full_time"] = full_timestamp
            await self._save_data()

            yield event.plain_result(
                f"当前体力：{current_stamina}/{max_stamina}\n"
                f"还需回复：{max_stamina - current_stamina} 点\n"
                f"预计回满时间：{full_datetime.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"所需总时长：{total_minutes // 60}小时{total_minutes % 60}分钟"
            )

        except ValueError:
            yield event.plain_result("输入的参数必须是数字。")
        except Exception as e:
            logger.error(f"[Stamina] 计算出错: {e}")
            yield event.plain_result("计算过程中发生错误。")

    @filter.command("火help")
    async def stamina_help(self, event: AstrMessageEvent):
        help_text = (
            "体力计算器帮助\n"
            "====================\n"
            "1. /上限 [25|50] : 设置体力上限\n"
            "2. /火 [体力值] [下点回复剩余分钟] : 计算回满时间\n"
            "3. /火 当前 : 查看当前倒计时\n"
            "4. /火help : 显示此帮助信息\n"
            "====================\n"
            f"当前设定回复速率：{self.config.get('recovery_interval_minutes', 30)}分钟/点"
        )
        yield event.plain_result(help_text)

    def terminate(self):
        self.executor.shutdown(wait=False)
        self._save_data_sync()
