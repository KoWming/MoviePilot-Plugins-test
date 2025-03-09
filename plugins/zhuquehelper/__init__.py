import re
import time
import requests
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional, Union, cast

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils


class ZhuqueHelper(_PluginBase):
    # 插件名称
    plugin_name = "朱雀助手"
    # 插件描述
    plugin_desc = "技能释放、一键升级、获取执行记录。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/zhuquehelper.png"
    # 插件版本
    plugin_version = "1.2"  # 更新版本号
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "zhuquehelper_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    # 任务执行间隔
    _cron: Optional[str] = None
    _cookie: Optional[str] = None
    _onlyonce: bool = False
    _notify: bool = False
    _history_days: Optional[int] = None
    _level_up: Optional[bool] = None
    _skill_release: Optional[bool] = None
    _target_level: Optional[int] = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """
        初始化插件
        """
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron")
            self._cookie = config.get("cookie")
            self._notify = config.get("notify", False)
            self._onlyonce = config.get("onlyonce", False)
            self._history_days = int(config.get("history_days", 15))
            self._level_up = config.get("level_up", False)
            self._skill_release = config.get("skill_release", False)
            self._target_level = int(config.get("target_level", 79))

        if self._onlyonce:
            try:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("朱雀助手服务启动，立即运行一次")
                if self._scheduler:
                    self._scheduler.add_job(
                        func=self.__signin, 
                        trigger='date',
                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                        name="朱雀助手"
                    )
                    # 关闭一次性开关
                    self._onlyonce = False
                    self.update_config({
                        "onlyonce": False,
                        "cron": self._cron,
                        "enabled": self._enabled,
                        "cookie": self._cookie,
                        "notify": self._notify,
                        "history_days": self._history_days,
                        "level_up": self._level_up,
                        "skill_release": self._skill_release,
                        "target_level": self._target_level,
                    })

                    # 启动任务
                    if self._scheduler.get_jobs():
                        self._scheduler.print_jobs()
                        self._scheduler.start()
            except Exception as e:
                logger.error(f"朱雀助手服务启动失败: {str(e)}")

    def __signin(self) -> None:
        """
        执行请求任务
        """
        if not self._cookie:
            logger.error("朱雀助手: Cookie未设置，无法执行任务")
            return

        try:
            # 获取CSRF令牌
            csrf_token = self._get_csrf_token()
            if not csrf_token:
                return

            # 设置请求头
            headers = self._create_headers(csrf_token)
            
            # 获取用户信息
            username = self._get_username(headers)
            if not username:
                return
                
            # 开始执行主要功能
            logger.info("开始获取用户信息...")
            bonus, min_level = self.get_user_info(headers)
            logger.info(f"获取用户信息完成，bonus: {bonus}, min_level: {min_level}")

            # 执行角色升级
            logger.info("开始一键升级角色...")
            results = self.train_genshin_character(
                self._target_level or 79, 
                self._skill_release or False, 
                self._level_up or False, 
                headers
            )
            logger.info(f"一键升级完成，结果: {results}")

            # 生成报告
            if bonus is not None and min_level is not None:
                logger.info("开始生成报告...")
                rich_text_report = self.generate_rich_text_report(results, bonus, min_level)
                logger.info(f"报告生成完成：\n{rich_text_report}")
            else:
                logger.error("获取用户信息失败，无法生成报告。")
                return

            # 保存执行记录
            self._save_execution_record(username, bonus, min_level, results)

            # 发送通知
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【朱雀助手任务执行完成】",
                    text=rich_text_report
                )

        except Exception as e:
            logger.error(f"朱雀助手任务执行失败: {str(e)}")

    def _get_csrf_token(self) -> Optional[str]:
        """获取CSRF令牌"""
        try:
            res = RequestUtils(cookies=self._cookie).get_res(url="https://zhuque.in/index")
            if not res or res.status_code != 200:
                logger.error("请求首页失败！状态码：%s", res.status_code if res else "无响应")
                return None

            pattern = r'<meta\s+name="x-csrf-token"\s+content="([^"]+)">'
            csrf_tokens = re.findall(pattern, res.text)
            if not csrf_tokens:
                logger.error("请求csrfToken失败！页面内容：%s", res.text[:500])
                return None

            csrf_token = csrf_tokens[0]
            logger.info(f"获取CSRF令牌成功：{csrf_token}")
            return csrf_token
        except requests.exceptions.RequestException as e:
            logger.error(f"获取CSRF令牌时发生异常: {e}")
            return None

    def _create_headers(self, csrf_token: str) -> Dict[str, str]:
        """创建请求头"""
        return {
            "cookie": self._cookie or "",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "x-csrf-token": csrf_token,
        }

    def _get_username(self, headers: Dict[str, str]) -> Optional[str]:
        """获取用户名"""
        try:
            res = RequestUtils(headers=headers).get_res(url="https://zhuque.in/api/user/getMainInfo")
            if not res or res.status_code != 200:
                logger.error("请求用户信息失败！状态码：%s，响应内容：%s", 
                             res.status_code if res else "无响应", 
                             res.text if res else "")
                return None

            data = res.json().get('data', {})
            username = data.get('username')
            if not username:
                logger.error("获取用户名失败！响应内容：%s", res.text)
                return None

            logger.info(f"获取用户名成功：{username}")
            return username
        except requests.exceptions.RequestException as e:
            logger.error(f"获取用户名时发生异常: {e}")
            return None
        except ValueError as e:
            logger.error(f"解析用户信息JSON时发生异常: {e}")
            return None

    def _save_execution_record(self, username: str, bonus: int, min_level: int, results: Dict) -> None:
        """保存执行记录"""
        try:
            sign_dict = {
                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                "username": username,
                "bonus": bonus,
                "min_level": min_level,
                "skill_release_bonus": results.get('skill_release', {}).get('bonus', 0),
            }

            # 读取历史记录
            history = self.get_data('sign_dict') or []
            if not isinstance(history, list):
                logger.error(f"历史记录格式不正确，重置为空列表。当前类型: {type(history)}")
                history = []
                
            history.append(sign_dict)
            
            # 清理过期记录
            if self._history_days:
                thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                history = [record for record in history if
                          datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
            
            self.save_data(key="sign_dict", value=history)
        except Exception as e:
            logger.error(f"保存执行记录时发生异常: {e}")

    def get_user_info(self, headers: Dict[str, str]) -> Tuple[Optional[int], Optional[int]]:
        """
        获取用户信息（灵石余额和角色最低等级）
        """
        url = "https://zhuque.in/api/gaming/listGenshinCharacter"
        try:
            response = RequestUtils(headers=headers).get_res(url=url)
            if not response or response.status_code != 200:
                logger.error(f"获取用户信息失败，状态码: {response.status_code if response else '无响应'}")
                return None, None
                
            data = response.json().get('data', {})
            if not data:
                logger.error("获取用户信息失败，返回数据为空")
                return None, None
                
            bonus = data.get('bonus')
            characters = data.get('characters', [])
            
            if not characters:
                logger.error("获取角色信息失败，角色列表为空")
                return bonus, None
                
            min_level = min(char.get('info', {}).get('level', 0) for char in characters)
            return bonus, min_level
        except requests.exceptions.RequestException as e:
            logger.error(f"获取用户信息失败: {e}")
            return None, None
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"解析用户信息时发生异常: {e}")
            return None, None

    def train_genshin_character(self, level: int, skill_release: bool, level_up: bool, 
                               headers: Dict[str, str]) -> Dict[str, Any]:
        """
        训练角色（释放技能和升级）
        """
        results: Dict[str, Any] = {}
        
        # 释放技能
        if skill_release:
            results['skill_release'] = self._release_skill(headers)
            
        # 一键升级
        if level_up:
            results['level_up'] = self._level_up_character(level, headers)
            
        return results

    def _release_skill(self, headers: Dict[str, str]) -> Dict[str, Any]:
        """释放技能"""
        url = "https://zhuque.in/api/gaming/fireGenshinCharacterMagic"
        data = {
            "all": 1,
            "resetModal": True
        }
        try:
            response = RequestUtils(headers=headers).post_res(url=url, json=data)
            if not response or response.status_code != 200:
                return {'status': '失败', 'error': f'状态码: {response.status_code if response else "无响应"}'}
                
            response_data = response.json()
            bonus = response_data.get('data', {}).get('bonus', 0)
            return {
                'status': '成功',
                'bonus': bonus
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"释放技能失败: {e}")
            return {'status': '失败', 'error': '网络错误'}
        except (ValueError, KeyError) as e:
            logger.error(f"解析释放技能响应时发生异常: {e}")
            return {'status': '失败', 'error': '解析响应失败'}

    def _level_up_character(self, level: int, headers: Dict[str, str]) -> Dict[str, Any]:
        """升级角色"""
        url = "https://zhuque.in/api/gaming/trainGenshinCharacter"
        data = {
            "resetModal": False,
            "level": level,
        }
        try:
            response = RequestUtils(headers=headers).post_res(url=url, json=data)
            if not response:
                return {'status': '失败', 'error': '无响应'}
                
            if response.status_code == 200:
                return {'status': '成功'}
            elif response.status_code == 400:
                return {'status': '成功', 'error': '灵石不足'}
            else:
                return {'status': '失败', 'error': f'状态码: {response.status_code}'}
        except requests.exceptions.RequestException as e:
            logger.error(f"升级角色失败: {e}")
            return {'status': '失败', 'error': '网络错误'}

    def generate_rich_text_report(self, results: Dict[str, Any], bonus: int, min_level: int) -> str:
        """生成报告"""
        try:
            report = "🌟 朱雀助手 🌟\n"
            report += f"技能释放：{'✅ ' if self._skill_release else '❌ '}\n"
            if 'skill_release' in results:
                if results['skill_release']['status'] == '成功':
                    report += f"成功，本次释放获得 {results['skill_release'].get('bonus', 0)} 灵石 💎\n"
                else:
                    report += f"失败，{results['skill_release'].get('error', '未知错误')} ❗️\n"
            report += f"一键升级：{'✅' if self._level_up else '❌'}\n"
            if 'level_up' in results:
                if results['level_up']['status'] == '成功':
                    if 'error' in results['level_up']:
                        report += f"升级受限，{results['level_up']['error']} ⚠️\n"
                    else:
                        report += f"升级成功 🎉\n"
                else:
                    report += f"失败，{results['level_up'].get('error', '未知错误')} ❗️\n"
            report += f"当前角色最低等级：{min_level} \n"
            report += f"当前账户灵石余额：{bonus} 💎\n"
            return report
        except Exception as e:
            logger.error(f"生成报告时发生异常: {e}")
            return "🌟 朱雀助手 🌟\n生成报告时发生错误，请检查日志以获取更多信息。"

    def get_state(self) -> bool:
        """获取插件状态"""
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取命令"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """获取API"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [{
                "id": "ZhuqueHelper",
                "name": "朱雀助手",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__signin,
                "kwargs": {}
            }]
        return []

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
                                    'md': 2
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'skill_release',
                                            'label': '技能释放',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 5
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'target_level',
                                            'label': '角色最高等级'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 5
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '站点cookie'
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
                                    'md': 2
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'level_up',
                                            'label': '一键升级',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 5
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 5
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '保留历史天数'
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
                                            'text': '特别鸣谢 Mr.Cai 大佬，插件源码来自于他的脚本。'
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
            "notify": False,
            "level_up": False,
            "skill_release": False,
            "cookie": "",
            "history_days": 15,
            "cron": "0 9 * * *",
            "target_level": 79,
        }

    def get_page(self) -> List[dict]:
        """获取页面配置"""
        # 查询同步详情
        historys = self.get_data('sign_dict')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            logger.error(f"历史记录格式不正确，类型为: {type(historys)}")
            return [
                {
                    'component': 'div',
                    'text': '数据格式错误，请检查日志以获取更多信息。',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        # 按照签到时间倒序
        historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)

        # 签到消息
        sign_msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("date", "")
                    },
                    {
                        'component': 'td',
                        'text': history.get("username", "")
                    },
                    {
                        'component': 'td',
                        'text': history.get("min_level", "")
                    },
                    {
                        'component': 'td',
                        'text': f"{history.get('skill_release_bonus', 0)} 💎"
                    },
                    {
                        'component': 'td',
                        'text': f"{history.get('bonus', 0)} 💎"
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
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
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '用户名'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '当前角色最低等级'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '本次释放获得的灵石'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '当前账户灵石余额'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': sign_msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self) -> None:
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
