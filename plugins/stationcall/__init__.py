import glob
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils
from urllib.parse import urlparse, urlunparse


class StationCall(_PluginBase):
    # 插件名称
    plugin_name = "站点喊话"
    # 插件描述
    plugin_desc = "特定站点喊话领取奖励。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/Lucky_B.png"
    # 插件版本
    plugin_version = "0.5.8"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "stationcall_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _site_urls: list[Dict] = []
    _site_cookies: list[Dict] = []
    _sites_room: list[Dict] = []

    _site_urls: str = ""
    _site_cookies: str = ""
    _sites_room: str = ""

    # 任务执行间隔
    _cron = None
    _onlyonce = False
    _notify = False

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._site_urls = config.get("urls")
            self._site_cookies = config.get("cookies")
            self._sites_room = config.get("room")

            # 加载模块
            self._site_urls = self.parse_site_urls(self._site_urls)
            self._site_cookies = self.parse_site_cookies(self._site_cookies)
            self._sites_room = self.parse_sites_room(self._sites_room)

            # 合并解析后的信息
            self._merged_sites = self.merge_site_info(self._site_urls, self._site_cookies, self._sites_room)

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"站点喊话服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__backup, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点喊话")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "cron": self._cron,
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "urls": config.get("urls"),
                    "cookies": config.get("cookies"),
                    "room": config.get("room"),
                })

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._enabled and self._cron:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"站点喊话服务启动，定时任务: {self._cron}")
                self._scheduler.add_job(func=self.main, trigger=CronTrigger.from_crontab(self._cron),
                                        name="站点喊话定时服务")
                self._scheduler.start()

    def parse_site_urls(self, site_urls: str) -> list[Dict]:
        """
        解析站点URLs
        """
        sites = []
        for line in site_urls.strip().split('\n'):
            if line:
                name, url = line.split('|', 1)
                sites.append({"name": name.strip(), "url": url.strip()})
        return sites

    def parse_site_cookies(self, site_cookies: str) -> list[Dict]:
        """
        解析站点Cookies
        """
        cookies = []
        for line in site_cookies.strip().split('\n'):
            if line:
                name, cookie = line.split('|', 1)
                cookies.append({"name": name.strip(), "cookie": cookie.strip()})
        return cookies

    def parse_sites_room(self, sites_room: str) -> list[Dict]:
        """
        解析站点聊天内容
        """
        rooms = []
        for line in sites_room.strip().split('\n'):
            if line:
                parts = line.split('|')
                name = parts[0].strip()
                messages = [msg.strip() for msg in parts[1:]]
                rooms.append({"name": name, "messages": messages})
        return rooms

    def merge_site_info(self, site_urls: list[Dict], site_cookies: list[Dict], sites_room: list[Dict]) -> Dict:
        """
        合并站点信息
        """
        merged_sites = {}
        for site in site_urls:
            name = site["name"]
            url = site["url"]
            parsed_url = urlparse(url)
            domain = f"{parsed_url.scheme}://{parsed_url.netloc}/"  # 重新组合完整的 URL
            merged_sites[name] = {
                "enabled": self._enabled,
                "url": url,
                "cookie_env": "",
                "referer": domain,  # 设置 referer 为完整的 URL
                "messages": []
            }

        for cookie in site_cookies:
            name = cookie["name"]
            if name in merged_sites:
                merged_sites[name]["cookie_env"] = cookie["cookie"]

        for room in sites_room:
            name = room["name"]
            if name in merged_sites:
                merged_sites[name]["messages"] = room["messages"]

        return merged_sites

    def send_message(self, site_config: Dict, message: str) -> bool:
        """
        通用的消息发送函数
        """
        if not site_config['enabled']:
            logger.info(f"{site_config['url']} 未启用")
            return False

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Cookie': os.getenv(site_config['cookie_env'], '').strip(),
            'Referer': site_config['referer'],
        }
        params = {
            'shbox_text': message,
            'shout': '我喊',
            'sent': 'yes',
            'type': 'shoutbox'
        }

        try:
            response = RequestUtils(headers=headers).get_res(site_config['url'], params=params)
            if response.status_code == 200:
                logger.info(f"成功向 {site_config['url']} 发送消息: {message}")
                return True  # 喊话成功
            else:
                logger.error(f"向 {site_config['url']} 发送消息失败: {response.status_code} - {response.text}")
                return False  # 喊话失败
        except Exception as e:
            logger.error(f"发送消息到 {site_config['url']} 时发生错误: {e}")
            return False  # 喊话失败

    def main(self):
        """
        主函数，遍历所有站点并发送消息
        """
        # 存储所有站点的喊话结果
        results = []

        # 遍历所有站点并发送消息
        for site_name, site_config in self._merged_sites.items():
            if not site_config['enabled']:
                logger.info(f"{site_name} 未启用")
                continue

            logger.info(f"开始处理站点: {site_name}")
            all_success = True  # 标记该站点是否所有消息都成功

            for i, message in enumerate(site_config['messages']):
                success = self.send_message(site_config, message)
                if not success:
                    all_success = False  # 如果有任何一条消息失败，标记为失败

                if i < len(site_config['messages']) - 1:  # 不需要在最后一条消息之后等待
                    logger.info(f"等待 {settings.GLOBAL_INTERVAL} 秒...")
                    time.sleep(settings.GLOBAL_INTERVAL)

            # 根据 all_success 决定站点的整体状态
            status = "喊话成功" if all_success else "喊话失败"
            results.append(f"{site_name} {status}")  # 格式化当前站点的结果

        # 构建最终的消息内容
        title = "---站点喊话---"
        content = "\n".join(results)

        # 发送通知
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【LuckyHelper备份完成】:",
                text=f"备份{'成功' if success else '失败'}\n"
                    f"获取到 {bk_path}\n路径下备份文件数量: {bk_cnt}\n"
                    f"清理备份数量: {del_cnt}\n"
                    f"剩余备份数量: {bk_cnt - del_cnt}\n"
                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"
            )

        return success

    def get_state(self) -> bool:
        return self._enabled     

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "StationCall",
                "name": "站点喊话定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__backup,
                "kwargs": {}
            }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'site_urls',
                                            'label': '站点列表',
                                            'rows': 5,
                                            'placeholder': '每一行一个站点，配置方式见下方提示。'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'site_cookies',
                                            'label': '站点Cookie',
                                            'rows': 5,
                                            'placeholder': '每一行一个Cookie，配置方式见下方提示。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'site_room',
                                            'label': '聊天内容',
                                            'rows': 5,
                                            'placeholder': '每一行一个内容，配置方式见下方提示。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 12
                        },
                        'content': [
                            {
                                'component': 'VCronField',
                                'props': {
                                    'model': 'cron',
                                    'label': '执行周期',
                                    'placeholder': '0 8 * * *',
                                    'hint': '输入5位cron表达式，默认每天8点运行。',
                                    'persistent-hint': True
                                }
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '站点列表: \n qingwa: \'https://www.***.com/shoutbox.php\'     \n'
                                            '站点Cookie: \n qingwa: \'cookie\'     \n'
                                            '多行使用,号连接。\n'
                                }
                            }
                        ]
                    }
                ]
            },
            ], {
                "enabled": False,
                "notify": False,
                "onlyonce": False,
                "cron": "0 8 * * *",
                "site_urls": "",
                "site_cookies": "",
                "site_room": "",
            }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))