from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, At
import datetime
from typing import Optional, Tuple

@register("astrbot_plugin_stamina_calculator", "Your Name", 
         "A plugin for calculating stamina recovery time", "1.0.0", 
         "https://github.com/yourusername/astrbot_plugin_stamina_calculator")
class StaminaCalculator(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        logger.info("Stamina Calculator plugin initialized")

    @filter.command("火")
    async def calculate_stamina(self, event: AstrMessageEvent, current_stamina: str, cooldown: str = "0"):
        """
        计算体力恢复时间
        用法: /火 [当前体力] [冷却时间]
        示例: /火 20 30
        """
        try:
            # 获取配置值
            stamina_limit = self.config.get("stamina_limit", 50)
            recovery_rate = self.config.get("recovery_rate", 30)
            time_format = self.config.get("time_format", "HH:mm")
            mention_user = self.config.get("mention_user", True)
            custom_message = self.config.get("custom_message", 
                                           "{user}，你的体力将在{day}{time}回满")
            max_stamina_warning = self.config.get("max_stamina_warning", True)
            invalid_input_msg = self.config.get("invalid_input_message", 
                                              "请输入有效的数字参数，示例：/火 20 30")

            # 验证输入参数
            try:
                current = int(current_stamina)
                cool = int(cooldown)
            except ValueError:
                yield event.plain_result(invalid_input_msg)
                return

            # 检查当前体力是否超过上限
            if max_stamina_warning and current > stamina_limit:
                yield event.plain_result(f"当前体力值超过上限{stamina_limit}点！")
                return

            # 计算恢复时间
            remaining = stamina_limit - current
            if remaining <= 0:
                yield event.plain_result("你的体力已经满了！")
                return

            total_minutes = remaining * recovery_rate + cool
            recovery_time = datetime.datetime.now() + datetime.timedelta(minutes=total_minutes)

            # 格式化输出
            day_str = "今天" if recovery_time.date() == datetime.datetime.now().date() else "明天"
            
            if time_format == "h:mm A":
                time_str = recovery_time.strftime("%I:%M %p")
            else:
                time_str = recovery_time.strftime("%H:%M")

            # 构建消息链
            user_name = event.get_sender_name()
            message = custom_message.format(
                user=user_name,
                time=time_str,
                day=day_str
            )

            chains = []
            if mention_user:
                chains.append(At(qq=event.get_sender_id()))
            chains.append(Plain(message))

            yield event.chain_result(chains)

        except Exception as e:
            logger.error(f"计算体力恢复时间时出错: {str(e)}")
            yield event.plain_result("计算体力恢复时间时发生错误，请稍后再试")

    async def terminate(self):
        '''插件卸载时调用'''
        logger.info("Stamina Calculator plugin terminated")