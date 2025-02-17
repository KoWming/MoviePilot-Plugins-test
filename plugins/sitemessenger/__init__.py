import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils

class SiteMessenger(_PluginBase):
    # 修改插件元数据
    plugin_name = "站点消息助手"
    plugin_desc = "定时向多个站点发送预设消息"
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/Lucky_B.png"
    plugin_version = "1.5"
    plugin_author = "KoWming"
    author_url = "https://github.com/KoWming"
    plugin_config_prefix = "sitemessenger_"
    plugin_order = 15
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _interval = 5
    _notify = False
    _onlyonce = False
    _sites = []

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._interval = int(config.get("interval", 5))
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._sites = config.get("sites", [])

        if self._enabled:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("消息发送服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.__send_messages,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3)
                )
                self._onlyonce = False
                self.update_config(config)

            if self._scheduler:
                self._scheduler.start()

    def __send_messages(self):
        """执行消息发送任务"""
        results = []
        for site in self._sites:
            if not site.get("enabled"):
                continue

            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Cookie": site.get("cookie", ""),
                    "Referer": site.get("referer", "")
                }

                for message in site.get("messages", []):
                    params = {
                        "shbox_text": message,
                        "shout": "我喊",
                        "sent": "yes",
                        "type": "shoutbox"
                    }

                    response = RequestUtils(headers=headers).get_res(
                        url=site["url"],
                        params=params
                    )

                    result = {
                        "site": site["name"],
                        "message": message,
                        "status": response.status_code if response else "请求失败",
                        "success": response.status_code == 200 if response else False
                    }
                    results.append(result)
                    
                    time.sleep(self._interval)

            except Exception as e:
                logger.error(f"发送消息到站点 {site.get('name')} 失败: {str(e)}")
                results.append({
                    "site": site.get("name"),
                    "message": "N/A",
                    "status": str(e),
                    "success": False
                })

        # 发送通知
        if self._notify:
            success_count = sum(1 for r in results if r["success"])
            failure_count = len(results) - success_count
            notification = (
                f"消息发送任务完成\n"
                f"成功: {success_count} 条\n"
                f"失败: {failure_count} 条\n"
                f"详细结果:\n" + "\n".join(
                    [f"{r['site']} - {r['message']}: {'成功' if r['success'] else '失败'} ({r['status']})" 
                     for r in results]
                )
            )
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【站点消息助手】",
                text=notification
            )

        return True

    def get_state(self) -> bool:
        return self._enabled
    
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "SiteMessenger",
                "name": "站点消息定时发送",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__send_messages,
                "kwargs": {}
            }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 生成站点配置行
        site_rows = []

        for index, site in enumerate(self._sites):
            site_rows.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VSwitch',
                                'props': {'model': f'sites[{index}].enabled', 'label': '启用站点'}
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {'model': f'sites[{index}].name', 'label': '站点名称'}
                            }
                        ]
                    }
                ]
            })
            site_rows.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {'model': f'sites[{index}].url', 'label': 'URL'}
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': f'sites[{index}].cookie',
                                    'label': 'Cookie',
                                    'type': 'password'
                                }
                            }
                        ]
                    }
                ]
            })
            site_rows.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {'model': f'sites[{index}].referer', 'label': 'Referer'}
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': f'sites[{index}].messages',
                                    'label': '消息内容',
                                    'placeholder': '多个消息用 | 分隔'
                                }
                            }
                        ]
                    }
                ]
            })

        # 添加"新增站点"按钮行
        # 由于无法使用动态按钮，这里不添加动态按钮，需要手动添加站点配置
        # 可以考虑在配置文件中手动添加站点配置

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {'model': 'enabled', 'label': '启用插件'}
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {'model': 'notify', 'label': '开启通知'}
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {'model': 'onlyonce', 'label': '立即运行一次'}
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 9 * * *',
                                            'hint': '设置定时任务执行时间'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '发送间隔(秒)',
                                            'type': 'number',
                                            'hint': '消息之间的发送间隔时间'
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
                                'props': {'cols': 12},
                                'content': site_rows
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cron": "0 9 * * *",
            "interval": 5,
            "notify": True,
            "onlyonce": False,
            "sites": [],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """停止服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败: {str(e)}")