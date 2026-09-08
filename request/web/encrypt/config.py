import configparser


class Config:
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.config.read('request/web/encrypt/web_encrypt_config.ini', encoding='utf-8')
    
    def get(self, section: str, key: str, fallback=None):
        """
        获取配置项的通用方法
        """
        return eval(self.config.get(section, key, fallback=fallback))
    
# 单例模式
xhs_config = Config()