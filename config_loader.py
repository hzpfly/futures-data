"""
共享配置加载模块
从 config.ini 读取天勤账号等敏感信息，避免硬编码在脚本中。
"""
import configparser
import os


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


def load_config():
    """读取 config.ini，返回 ConfigParser 对象。"""
    cfg = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(
            f"配置文件 {CONFIG_FILE} 不存在。\n"
            f"请复制 config.example.ini 为 config.ini 并填入你的天勤账号。"
        )
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def get_tqsdk_auth():
    """返回 (username, password) 元组。"""
    cfg = load_config()
    username = cfg.get("tqsdk", "username")
    password = cfg.get("tqsdk", "password")
    return username, password
