from lxml import etree
from typing import List, Dict, Any, Callable, Optional, Union
from app.log import logger

class NexusPHPHelper:
    """NexusPHP站点操作增强工具类"""
    
    def __init__(self, site_info: dict, request_helper):
        """
        初始化NexusPHP站点操作工具
        :param site_info: 站点信息字典，包含url/cookie/ua等
        :param request_helper: 请求工具类实例
        """
        # 初始化基础配置
        self.url = site_info.get('url', '').rstrip('/')
        self.cookie = site_info.get('cookie', '')
        self.ua = site_info.get('ua', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0')
        self.base_headers = {
            "Cookie": self.cookie,
            "Referer": self.url,
            "User-Agent": self.ua
        }
        
        # 初始化API端点
        self.endpoints = {
            'shoutbox': f"{self.url}/shoutbox.php",
            'messages': f"{self.url}/messages.php",
        }
        
        # 请求工具类
        self.request_helper = request_helper

    def send_message(self, message: str) -> str:
        """
        发送群聊消息
        :param message: 要发送的消息内容
        :return: 操作结果描述
        """
        params = {
            "shbox_text": message,
            "shout": "我喊",
            "sent": "yes",
            "type": "shoutbox"
        }
        
        try:
            response = self.request_helper.request(
                method="GET",
                url=self.endpoints['shoutbox'],
                params=params,
                headers=self.base_headers,
                timeout=15
            )
            
            # 解析响应结果
            def parser(response):
                html = etree.HTML(response.text)
                if html is not None:
                    elements = html.xpath("//tr[1]/td//text()")
                    return " ".join(elements) if elements else "无响应内容"
                return "解析失败：无法解析HTML"
                
            return self._parse_response(response, parser)
        except Exception as e:
            logger.error(f"消息发送失败: {str(e)}")
            return f"失败: {str(e)}"

    def get_messages(self, count: int = 10) -> list:
        """
        获取最新群聊消息
        :param count: 获取消息条数
        :return: 消息列表
        """
        try:
            response = self.request_helper.request(
                method="GET",
                url=self.endpoints['shoutbox'],
                headers=self.base_headers,
                timeout=10
            )
            
            def parser(response):
                html = etree.HTML(response.text)
                if html is not None:
                    elements = html.xpath("//tr/td")
                    return ["".join(item.xpath(".//text()")) for item in elements[:count]]
                return []
                
            result = self._parse_response(response, parser)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"获取消息失败: {str(e)}")
            return []

    def get_message_list(self, rt_method: Optional[Callable] = None) -> list:
        """
        获取站内信列表
        :param rt_method: 自定义解析方法
        :return: 消息列表
        """
        try:
            response = self.request_helper.request(
                method="GET",
                url=self.endpoints['messages'],
                headers=self.base_headers
            )
            
            # 打印响应内容以调试
            logger.debug(f"响应内容: {response.text[:500]}")  # 打印前500个字符
            
            # 默认解析逻辑
            if not rt_method:
                def default_parser(response):
                    html = etree.HTML(response.text)
                    if html is not None:
                        elements = html.xpath("//form/table//tr")
                        return [
                            {
                                "status": "".join(item.xpath("./td[1]/img/@title") or [""]),
                                "topic": "".join(item.xpath("./td[2]//text()") or [""]),
                                "from": "".join(item.xpath("./td[3]/text()") or [""]),
                                "time": "".join(item.xpath("./td[4]//text()") or [""]),
                                "id": "".join(item.xpath("./td[5]/input/@value") or [""])
                            }
                            for item in elements
                        ]
                    return []
                
                rt_method = default_parser
                
            message_list = rt_method(response)
            
            # 打印解析结果以调试
            logger.debug(f"解析结果: {message_list}")
            
            return message_list
        except Exception as e:
            logger.error(f"获取站内信失败: {str(e)}")
            return []

    def set_message_read(self, message_id: str, rt_method: Optional[Callable] = None) -> bool:
        """
        标记站内信为已读
        :param message_id: 消息ID
        :param rt_method: 自定义解析方法
        :return: 操作是否成功
        """
        try:
            data = {
                "action": "moveordel",
                "messages[]": message_id,
                "markread": "设为已读",
                "box": "1"
            }
            
            response = self.request_helper.request(
                method="POST",
                url=self.endpoints['messages'],
                headers=self.base_headers,
                data=data
            )
            
            # 默认成功判断
            if not rt_method:
                rt_method = lambda response: response.status_code == 200
                
            return rt_method(response)
        except Exception as e:
            logger.error(f"标记已读失败: {str(e)}")
            return False

    def _parse_response(self, response, parser: Callable) -> Any:
        """
        统一响应解析方法
        :param response: 响应对象
        :param parser: 解析函数
        :return: 解析结果
        """
        try:
            return parser(response)
        except Exception as e:
            logger.error(f"响应解析失败: {str(e)}")
            return "响应解析失败" 