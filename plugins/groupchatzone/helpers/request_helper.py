import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from app.core.config import settings

class RequestHelper:
    """自定义请求工具类"""
    
    def __init__(self, plugin):
        """
        初始化请求工具类
        :param plugin: 插件实例，用于获取日志记录器
        """
        self.plugin = plugin
        self.logger = plugin.logger
        self.retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[403, 404, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        self.adapter = HTTPAdapter(max_retries=self.retries)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        统一请求方法
        :param method: 请求方法，如GET、POST
        :param url: 请求URL
        :param kwargs: 其他请求参数
        :return: 请求响应对象
        """
        # 合并代理配置
        proxies = kwargs.pop('proxies', None) or settings.PROXY
        
        # 配置默认超时
        timeout = kwargs.pop('timeout', (3.05, 10))
        
        # 创建会话
        with requests.Session() as session:
            session.mount('https://', self.adapter)
            session.mount('http://', self.adapter)  # 添加HTTP适配器
            session.proxies = proxies
            
            try:
                response = session.request(
                    method=method.upper(),
                    url=url,
                    timeout=timeout,
                    **kwargs
                )
                response.raise_for_status()
                self.logger.debug(f"请求成功: {method} {url}")
                return response
            except Exception as e:
                self.logger.error(f"请求失败: {method} {url} - {str(e)}")
                raise 