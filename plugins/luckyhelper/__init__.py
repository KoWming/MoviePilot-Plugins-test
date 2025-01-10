import glob
import os
import shutil
import time
import jwt
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
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
    _host = None
    _openToken = None

    # 任务执行间隔
    _cron = None
    _cnt = None
    _onlyonce = False
    _notify = False
    _back_path = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._cnt = config.get("cnt")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._back_path = config.get("back_path")

            # 加载模块
        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"自动备份服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__backup, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="自动备份")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config({
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "cnt": self._cnt,
                "notify": self._notify,
                "back_path": self._back_path,
            })

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def api_backup(self, apikey: str):
        """
        API调用备份
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        return self.__backup()

    def __backup(self):
        """
        自动备份、删除备份
        """
        logger.info(f"当前时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))} 开始备份")

        # 备份保存路径
        bk_path = Path(self._back_path) if self._back_path else self.get_data_path()

        # 构造请求URL
        backup_url = f"{self._host}/api/configure?openToken={self._openToken}"

        try:
            # 发送GET请求获取ZIP文件
            result = (RequestUtils(headers={"Authorization": self.get_jwt()})
                    .get_res(backup_url))
            
            # 检查响应状态码
            if result.status_code == 200:
                # 获取响应内容（ZIP文件的二进制数据）
                zip_data = result.content
                
                # 定义保存文件的路径，使用原始文件名
                zip_file_name = result.headers.get('Content-Disposition', '').split('filename=')[-1].strip('"')
                if not zip_file_name:
                    zip_file_name = f"bk_{time.strftime('%Y%m%d%H%M%S')}.zip"
                zip_file_path = bk_path / zip_file_name
                
                # 保存文件到本地
                with open(zip_file_path, 'wb') as zip_file:
                    zip_file.write(zip_data)
                
                success = True
                msg = f"备份完成 备份文件 {zip_file_path}"
                logger.info(msg)
            else:
                success = False
                msg = f"创建备份失败，状态码: {result.status_code}, 原因: {result.json().get('msg', '未知错误')}"
                logger.error(msg)
        except Exception as e:
            success = False
            msg = f"创建备份失败，异常: {str(e)}"
            logger.error(msg)

        # 清理备份
        bk_cnt = 0
        del_cnt = 0
        if self._cnt:
            # 获取指定路径下所有以"bk"开头的文件，按照创建时间从旧到新排序
            files = sorted(glob.glob(f"{bk_path}/bk**.zip"), key=os.path.getctime)
            bk_cnt = len(files)
            # 计算需要删除的文件数
            del_cnt = bk_cnt - int(self._cnt)
            if del_cnt > 0:
                logger.info(
                    f"获取到 {bk_path} 路径下备份文件数量 {bk_cnt} 保留数量 {int(self._cnt)} 需要删除备份文件数量 {del_cnt}")

                # 遍历并删除最旧的几个备份
                for i in range(del_cnt):
                    os.remove(files[i])
                    logger.debug(f"删除备份文件 {files[i]} 成功")
            else:
                logger.info(
                    f"获取到 {bk_path} 路径下备份文件数量 {bk_cnt} 保留数量 {int(self._cnt)} 无需删除")

        # 发送通知
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【自动备份任务完成】",
                text=f"创建备份{'成功' if success else '失败'}\n"
                    f"清理备份数量 {del_cnt}\n"
                    f"剩余备份数量 {bk_cnt - del_cnt}")

        return success, msg

    @staticmethod
    def backup_file(bk_path: Path = None):
        """
        @param bk_path     自定义备份路径
        """
        try:
            # 创建备份文件夹
            config_path = Path(settings.CONFIG_PATH)
            backup_file = f"bk_{time.strftime('%Y%m%d%H%M%S')}"
            backup_path = bk_path / backup_file
            backup_path.mkdir(parents=True)
        except IOError:
            return None

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/backup",
            "endpoint": self.api_backup,
            "methods": ["GET"],
            "summary": "MoviePilot备份",
            "description": "MoviePilot备份",
        }]

    def get_jwt(self) -> str:
        # 减少接口请求直接使用jwt
        payload = {
            "exp": int(time.time()) + 28 * 24 * 60 * 60,
            "iat": int(time.time())
        }
        encoded_jwt = jwt.encode(payload, self._secretKey, algorithm="HS256")
        logger.debug(f"LuckyHelper get jwt---》{encoded_jwt}")
        return "Bearer "+encoded_jwt

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
                "id": "AutoBackup",
                "name": "自动备份定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__backup,
                "kwargs": {}
            }]

    def backup(self) -> schemas.Response:
        """
        API调用备份
        """
        success, msg = self.__backup()
        return schemas.Response(
            success=success,
            message=msg
        )

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
                        ]
                    },
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '备份周期',
                                            'placeholder': '0 7 * * *',
                                            'hint': '输入5位cron表达式',
                                            'persistent-hint': True
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'back_path',
                                            'label': '备份保存路径',
                                            'hint': '自定义备份路径',
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
            "request_method": "POST",
            "webhook_url": "",
            "back_path": str(self.get_data_path())
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