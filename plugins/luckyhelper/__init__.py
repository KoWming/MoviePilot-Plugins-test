from datetime import datetime, timedelta

from typing import Optional, Any, List, Dict, Tuple
import time
import pytz
import jwt
import requests
from requests import Session, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.core.event import eventmanager, Event

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class LuckyHelper(_PluginBase):
    # 插件名称
    plugin_name = "Lucky助手"
    # 插件描述
    plugin_desc = "配合Lucky完成自动备份"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins-test/main/icons/Lucky_B.png"
    # 插件版本
    plugin_version = "1.7"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "luckyhelper_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False

    # 备份
    _backup_cron = None
    _backups_notify = False
    _host = None
    _openToken = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        if config:
            self._backup_cron = config.get("backupcron")
            self._backups_notify = config.get("backupsnotify")
            self._host = config.get("host")
            self._openToken = config.get("openToken")

            # 加载模块
            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Lucky助手服务启动，立即运行一次")
                    if self._backup_cron:
                        self._scheduler.add_job(self.backup, 'date',
                                                run_date=datetime.now(
                                                    tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                                name="Lucky助手-备份")
                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()
                # 周期运行
                if self._backup_cron:
                    try:
                        self._scheduler.add_job(func=self.backup,
                                                trigger=CronTrigger.from_crontab(self._backup_cron),
                                                name="Lucky助手-备份")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{str(err)}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")
                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def backup(self):
        """
        备份
        """
        try:
            logger.info(f"Lucky-备份-准备执行")
            backup_url = '%s/api/configure?openToken=%s' % (self._host, self._openToken)
            result = (RequestUtils(headers={"Authorization": self.get_jwt()})
                    .get_res(backup_url))
            
            # 检查响应状态码
            if result.status_code == 200:
                # 假设响应内容是备份文件的二进制数据
                backup_data = result.content
                
                # 定义保存文件的路径
                backup_file_path = f"config/plugins/LuckyHelper/"
                
                # 保存文件到本地
                with open(backup_file_path, 'wb') as backup_file:
                    backup_file.write(backup_data)
                
                logger.info(f"Lucky-备份完成，文件保存到 {backup_file_path}")
                
                if self._backups_notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【Lucky助手-备份成功】",
                        text=f"备份成功！文件保存到 {backup_file_path}")
            else:
                if self._backups_notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【Lucky助手-备份失败】",
                        text=f"备份失败！\n【失败原因】:{result.json().get('msg', '未知错误')}")
                logger.error(f"Lucky-备份失败 Error code: {result.status_code}, message: {result.json().get('msg', '未知错误')}")
        except Exception as e:
            logger.error(f"Lucky-备份失败,网络异常,请检查Lucky服务是否正常: {str(e)}")
            return []

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass 

    def get_jwt(self) -> str:
        # 减少接口请求直接使用jwt
        payload = {
            "exp": int(time.time()) + 28 * 24 * 60 * 60,
            "iat": int(time.time())
        }
        encoded_jwt = jwt.encode(payload, self._secretKey, algorithm="HS256")
        logger.debug(f"LuckyHelper get jwt---》{encoded_jwt}")
        return "Bearer "+encoded_jwt

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据： 1、页面配置；2、数据结构
        """
        return [
            {
                "component": "VForm",
                "content": [
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
                                            'model': 'backupsnotify',
                                            'label': '备份通知',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'host',
                                            'label': 'Lucky地址',
                                            'hint': 'Lucky服务地址 http(s)://ip:prot',
                                            'persistent-hint': True
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'openToken',
                                            'label': 'openToken',
                                            'hint': 'Lucky openToken 设置里面打开',
                                            'persistent-hint': True
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cnt',
                                            'label': '保留份数',
                                            'hint': '最大保留备份数',
                                            'persistent-hint': True
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'backupcron',
                                            'label': '自动备份',
                                            'placeholder': '0 7 * * *',
                                            'hint': '输入5位cron表达式',
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
                                            'text': '备份文件路径默认为本地映射的config/plugins/LuckyHelper。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "backupsnotify": False,
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

