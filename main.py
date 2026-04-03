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
import astrbot.api.message_components as Comp

# 尝试导入 MessageChain
try:
    from astrbot.core.message.message_chain import MessageChain
except ImportError:
    try:
        from astrbot.api.message import MessageChain
    except ImportError:
        # 如果都导入失败，定义一个简单的 MessageChain 类
        class MessageChain(list):
            """简单的 MessageChain 兼容类"""
            @property
            def chain(self):
                return self


@register("stamina_calculator", "AstrBot-Assistant", "游戏体力回复计算助手。支持设置25或50体力上限，每30分钟回复1点，支持计算回满时间。", "1.0.4")
class StaminaCalculatorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "stamina_user_data.json"

        self.executor = ThreadPoolExecutor(max_workers=1)
        self.user_data = self._load_data_sync()
        self.reminder_tasks: Dict[str, asyncio.Task] = {}
        
        # 创建一个任务来恢复提醒（在事件循环运行后执行）
        asyncio.create_task(self._restore_reminder_tasks())

    def _load_data_sync(self) -> dict:
        """同步加载数据"""
        if self.db_path.exists():
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[Stamina] 加载数据失败: {e}")
        return {}

    async def _save_data(self):
        """异步保存数据"""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self.executor, self._save_data_sync)
        except Exception as e:
            logger.error(f"[Stamina] 异步保存数据失败: {e}")

    def _save_data_sync(self):
        """同步保存数据"""
        try:
            tmp_path = str(self.db_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, str(self.db_path))
        except Exception as e:
            logger.error(f"[Stamina] 物理写入数据失败: {e}")

    async def _get_user_info(self, user_id: str) -> dict:
        """获取用户信息"""
        if user_id not in self.user_data:
            self.user_data[user_id] = {
                "max_stamina": self.config.get("default_max_stamina", 25),
                "full_time": 0,
                "reminder_enabled": False,
                "unified_msg_origin": ""
            }
        return self.user_data[user_id]

    async def _restore_reminder_tasks(self):
        """恢复之前开启的提醒任务（程序重启后调用）"""
        await asyncio.sleep(0.5)
        restored_count = 0
        for user_id, data in self.user_data.items():
            full_time = data.get("full_time", 0)
            reminder_enabled = data.get("reminder_enabled", False)
            
            if reminder_enabled and full_time > time.time():
                await self._start_reminder_task(user_id)
                restored_count += 1
                logger.info(f"[Stamina] 已恢复用户 {user_id} 的提醒任务")
            elif reminder_enabled and full_time <= time.time():
                data["reminder_enabled"] = False
                await self._save_data()
                logger.info(f"[Stamina] 用户 {user_id} 的体力已回满，已自动关闭提醒")
        
        if restored_count > 0:
            logger.info(f"[Stamina] 共恢复了 {restored_count} 个提醒任务")

    @filter.command("上限")
    async def set_limit(self, event: AstrMessageEvent, limit: int):
        """设置体力上限"""
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
        """计算体力回复时间"""
        user_id = event.get_sender_id()
        info = await self._get_user_info(user_id)

        # 查询当前状态
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

        # 计算新任务
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

            # 存储会话标识和回满时间
            info["full_time"] = full_timestamp
            info["unified_msg_origin"] = event.unified_msg_origin
            await self._save_data()

            # 自动开启提醒功能
            await self._set_reminder(user_id, True)

            yield event.plain_result(
                f"当前体力：{current_stamina}/{max_stamina}\n"
                f"还需回复：{max_stamina - current_stamina} 点\n"
                f"预计回满时间：{full_datetime.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"所需总时长：{total_minutes // 60}小时{total_minutes % 60}分钟\n"
                f"提醒已自动开启，回满时会通知你。"
            )

        except ValueError:
            yield event.plain_result("输入的参数必须是数字。")
        except Exception as e:
            logger.error(f"[Stamina] 计算出错: {e}")
            yield event.plain_result("计算过程中发生错误。")

    @filter.command("火提醒")
    async def toggle_reminder(self, event: AstrMessageEvent, state: str = None):
        """开关体力提醒"""
        user_id = event.get_sender_id()
        info = await self._get_user_info(user_id)

        # 如果没有提供参数，显示当前状态
        if state is None or state not in ["on", "off"]:
            status = "开启" if info.get("reminder_enabled", False) else "关闭"
            full_time = info.get("full_time", 0)
            if info.get("reminder_enabled", False) and full_time > time.time():
                remaining_sec = full_time - time.time()
                rem_h = int(remaining_sec // 3600)
                rem_m = int((remaining_sec % 3600) // 60)
                yield event.plain_result(
                    f"当前提醒状态：{status}\n"
                    f"预计回满时间：{datetime.fromtimestamp(full_time).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"剩余时间：{rem_h}小时{rem_m}分钟\n"
                    f"使用 /火提醒 on 或 /火提醒 off 来开关提醒"
                )
            else:
                yield event.plain_result(
                    f"当前提醒状态：{status}\n"
                    f"使用 /火提醒 on 或 /火提醒 off 来开关提醒"
                )
            return

        enabled = (state == "on")
        await self._set_reminder(user_id, enabled)

        if enabled:
            full_time = info.get("full_time", 0)
            if full_time <= time.time():
                yield event.plain_result("当前没有进行中的体力恢复任务，请先使用 /火 命令计算体力。")
                await self._set_reminder(user_id, False)
            else:
                remaining_sec = full_time - time.time()
                rem_h = int(remaining_sec // 3600)
                rem_m = int((remaining_sec % 3600) // 60)
                yield event.plain_result(
                    f"体力提醒已开启！\n"
                    f"预计回满时间：{datetime.fromtimestamp(full_time).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"剩余时间：{rem_h}小时{rem_m}分钟"
                )
        else:
            yield event.plain_result("体力提醒已关闭。")

    @filter.command("火取消")
    async def cancel_reminder(self, event: AstrMessageEvent):
        """取消当前的体力恢复任务"""
        user_id = event.get_sender_id()
        info = await self._get_user_info(user_id)
        
        if info.get("full_time", 0) == 0:
            yield event.plain_result("当前没有进行中的体力恢复任务。")
            return
        
        # 清除任务数据
        info["full_time"] = 0
        if info.get("reminder_enabled", False):
            await self._set_reminder(user_id, False)
        await self._save_data()
        
        yield event.plain_result("已取消当前的体力恢复任务。")

    @filter.command("火help")
    async def stamina_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = (
            "🎮 体力计算器帮助\n"
            "====================\n"
            "1. /上限 [25|50] - 设置体力上限\n"
            "2. /火 [体力值] [剩余分钟] - 计算回满时间\n"
            "   示例：/火 10 15\n"
            "3. /火 当前 - 查看当前倒计时\n"
            "4. /火提醒 [on/off] - 开启/关闭提醒\n"
            "5. /火取消 - 取消当前任务\n"
            "6. /火help - 显示此帮助\n"
            "====================\n"
            f"⚡ 回复速率：{self.config.get('recovery_interval_minutes', 30)}分钟/点\n"
            "💡 提示：使用 /火 计算时会自动开启提醒"
        )
        yield event.plain_result(help_text)

    async def _check_stamina_reminder(self, user_id: str, user_info: dict):
        """检查体力是否回满，如果回满则提醒用户"""
        full_time = user_info.get("full_time", 0)
        if full_time == 0:
            return

        now_ts = time.time()
        if now_ts >= full_time:
            try:
                unified_msg_origin = user_info.get("unified_msg_origin", "")
                if not unified_msg_origin:
                    logger.error(f"[Stamina] 用户 {user_id} 没有存储会话标识，无法发送提醒")
                    return

                # 创建 MessageChain 对象
                message_chain = MessageChain()
                message_chain.append(Comp.At(qq=user_id))
                message_chain.append(Comp.Plain(f" 体力已回满！🎉\n当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
                
                await self.context.send_message(unified_msg_origin, message_chain)
                
                logger.info(f"[Stamina] 已向 {unified_msg_origin} 发送体力回满提醒")
                
                # 提醒发送后清除任务数据并关闭提醒
                user_info["full_time"] = 0
                user_info["reminder_enabled"] = False
                await self._save_data()
                
                # 停止提醒任务
                await self._stop_reminder_task(user_id)
                
            except Exception as e:
                logger.error(f"[Stamina] 发送提醒消息失败: {e}")
                import traceback
                logger.error(f"[Stamina] 详细错误: {traceback.format_exc()}")

    async def _start_reminder_task(self, user_id: str):
        """启动用户的提醒任务"""
        if user_id in self.reminder_tasks:
            task = self.reminder_tasks[user_id]
            if not task.done():
                return
            else:
                del self.reminder_tasks[user_id]

        async def reminder_loop():
            logger.info(f"[Stamina] 用户 {user_id} 的提醒任务已启动")
            while True:
                user_info = self.user_data.get(user_id)
                if not user_info or not user_info.get("reminder_enabled", False):
                    logger.info(f"[Stamina] 用户 {user_id} 的提醒已关闭，任务退出")
                    break

                await self._check_stamina_reminder(user_id, user_info)
                await asyncio.sleep(5)  # 每5秒检查一次

        self.reminder_tasks[user_id] = asyncio.create_task(reminder_loop())

    async def _stop_reminder_task(self, user_id: str):
        """停止用户的提醒任务"""
        task = self.reminder_tasks.get(user_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                if user_id in self.reminder_tasks:
                    del self.reminder_tasks[user_id]
                    logger.info(f"[Stamina] 用户 {user_id} 的提醒任务已停止")

    async def _set_reminder(self, user_id: str, enabled: bool):
        """设置提醒开关（私有方法）"""
        user_info = await self._get_user_info(user_id)
        
        old_state = user_info.get("reminder_enabled", False)
        if old_state == enabled:
            return
            
        user_info["reminder_enabled"] = enabled
        await self._save_data()

        if enabled:
            await self._start_reminder_task(user_id)
        else:
            await self._stop_reminder_task(user_id)

    def terminate(self):
        """插件终止时调用"""
        logger.info("[Stamina] 插件正在关闭，清理资源...")
        for user_id, task in self.reminder_tasks.items():
            if not task.done():
                task.cancel()
        self._save_data_sync()
        self.executor.shutdown(wait=False)
        logger.info("[Stamina] 插件已关闭")
