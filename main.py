import asyncio
import datetime
import aiosqlite
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

@register("stamina_calc", "AstrBot", "计算游戏体力（火）恢复时间并支持体力回满提醒，支持设置个人体力上限。", "1.1.0", "https://github.com/YourUsername/astrbot_plugin_stamina_calc")
class StaminaCalcPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 使用 StarTools 获取插件数据目录
        self.data_dir = StarTools.get_data_dir()
        self.db_path = self.data_dir / "user_data.db"
        self._loop = asyncio.get_event_loop()
        # 确保目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 异步初始化数据库
        asyncio.create_task(self._init_db())

    async def _init_db(self):
        """初始化数据库"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        max_stamina INTEGER,
                        remind_enabled INTEGER
                    )
                ''')
                await db.commit()
        except Exception as e:
            logger.error(f"StaminaCalc 数据库初始化失败: {e}")

    async def _get_user_data(self, user_id: str):
        """异步获取用户设置的上限和提醒状态"""
        default_limit = self.config.get("default_stamina_limit", 25)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT max_stamina, remind_enabled FROM users WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return row[0] if row[0] else default_limit, bool(row[1])
        except Exception as e:
            logger.error(f"StaminaCalc 获取用户数据失败: {e}")
        return default_limit, False

    async def _update_user_data(self, user_id: str, max_stamina: int = None, remind: bool = None):
        """异步更新用户信息"""
        default_limit = self.config.get("default_stamina_limit", 25)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT OR IGNORE INTO users (user_id, max_stamina, remind_enabled) VALUES (?, ?, ?)", 
                               (user_id, default_limit, 0))
                if max_stamina is not None:
                    await db.execute("UPDATE users SET max_stamina = ? WHERE user_id = ?", (max_stamina, user_id))
                if remind is not None:
                    await db.execute("UPDATE users SET remind_enabled = ? WHERE user_id = ?", (int(remind), user_id))
                await db.commit()
        except Exception as e:
            logger.error(f"StaminaCalc 更新用户数据失败: {e}")

    async def _schedule_reminder(self, event: AstrMessageEvent, delay_minutes: int, target_stamina: int):
        """实现定时提醒逻辑"""
        if delay_minutes <= 0:
            return
        
        # 转换为秒
        delay_seconds = delay_minutes * 60
        await asyncio.sleep(delay_seconds)
        
        # 再次确认用户是否开启了提醒
        _, remind_enabled = await self._get_user_data(event.get_sender_id())
        global_enable = self.config.get("enable_at_notification", True)
        
        if remind_enabled and global_enable:
            try:
                msg = f"🔔 体力回满提醒：\n您的体力已恢复至 {target_stamina} 点，快去上线清理吧！"
                await self.context.send_message(event.unified_msg_origin, msg)
            except Exception as e:
                logger.error(f"StaminaCalc 发送提醒失败: {e}")

    @filter.command("火")
    async def calc_stamina(self, event: AstrMessageEvent, current: int, next_point_min: int):
        """计算体力（火）回满时间。格式：/火 [当前值] [距离下一点恢复的时间(分)]"""
        user_id = event.get_sender_id()
        max_limit, remind_enabled = await self._get_user_data(user_id)
        recovery_interval = self.config.get("recovery_interval_minutes", 30)

        if current >= max_limit:
            yield event.plain_result(f"你的体力已经是满的啦（{current}/{max_limit}）。")
            return

        if next_point_min > recovery_interval or next_point_min < 0:
            yield event.plain_result(f"输入错误：距离下一点恢复时间应在 0-{recovery_interval} 分钟之间。")
            return

        # 计算剩余总时间
        remain_points = max_limit - current
        total_minutes = (remain_points - 1) * recovery_interval + next_point_min

        now = datetime.datetime.now()
        finish_time = now + datetime.timedelta(minutes=total_minutes)
        
        hours = total_minutes // 60
        mins = total_minutes % 60
        time_str = finish_time.strftime("%Y-%m-%d %H:%M:%S")
        
        res_msg = f"📊 体力计算结果：\n当前：{current}/{max_limit}\n回满还需：{hours}小时{mins}分钟\n预计回满时间：{time_str}"
        
        # 如果开启了提醒，创建异步任务
        if remind_enabled:
            asyncio.create_task(self._schedule_reminder(event, total_minutes, max_limit))
            res_msg += "\n⏰ 已为您创建回满提醒任务。"

        yield event.plain_result(res_msg)

    @filter.command("上限")
    async def set_limit(self, event: AstrMessageEvent, limit: int):
        """设置个人体力上限（支持25或50）。格式：/上限 [25/50]"""
        allowed_options = self.config.get("max_stamina_options", [25, 50])
        
        if limit not in allowed_options:
            yield event.plain_result(f"设置失败。目前仅支持设置为：{', '.join(map(str, allowed_options))}")
            return
        
        await self._update_user_data(event.get_sender_id(), max_stamina=limit)
        yield event.plain_result(f"设置成功！你的个人体力上限已更新为 {limit}。")

    @filter.command("提醒")
    async def toggle_remind(self, event: AstrMessageEvent, switch: str):
        """开启或关闭体力回满提醒。格式：/提醒 [on/off]"""
        is_on = switch.lower() == "on"
        is_off = switch.lower() == "off"
        
        if not is_on and not is_off:
            yield event.plain_result("格式错误。请输入 /提醒 on 或 /提醒 off")
            return
        
        await self._update_user_data(event.get_sender_id(), remind=is_on)
        status = "开启" if is_on else "关闭"
        
        global_enable = self.config.get("enable_at_notification", True)
        warning = "" if global_enable or not is_on else "\n⚠️ 注意：管理员当前已关闭全局提醒功能，你可能无法收到通知。"
        
        yield event.plain_result(f"提醒功能已{status}。{warning}")

    @filter.command("火help")
    async def stamina_help(self, event: AstrMessageEvent):
        """查看体力计算插件指令预览及说明"""
        help_text = self.config.get("help_menu_template", "体力计算助手使用说明：\n1. /火 [当前点数] [下点恢复剩余分钟]\n2. /上限 [25/50] 设置最大值\n3. /提醒 [on/off] 开关通知")
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载逻辑"""
        logger.info("StaminaCalc 插件已卸载。")
