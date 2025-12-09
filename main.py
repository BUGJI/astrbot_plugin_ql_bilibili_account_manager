import json  # 新增这行
import requests
import qrcode
import time
import io
import os
import tempfile
import shutil
from http.cookies import SimpleCookie
from typing import Dict, List, Tuple, Optional
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig

# 是B站登录相关API
QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_CHECK_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
HOME_PAGE_URL = "https://www.bilibili.com/"
CHECK_PREFIX = "Ray_BiliBiliCookies__"

@register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.ql_panel_url = self.config.ql_config.get("ql_panel_url", "").rstrip("/")
        self.ql_client_id = self.config.ql_config.get("ql_client_id", "")
        self.ql_client_secret = self.config.ql_config.get("ql_client_secret", "")
        self.ql_env_mapping = json.loads(self.config.slot_config.get("ql_env_mapping", "{}"))
        self.max_account = int(self.config.slot_config.get("max_account", 10))
        self.test = self.config.slot_config.get("test", False)
        # 会话保持
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        }
        logger.info(f"BiliTool插件初始化完成，配置：青龙地址={self.ql_panel_url}，最大账号数={self.max_account}，测试模式={self.test}")

    async def initialize(self):
        """异步初始化方法"""
        logger.info("BiliTool插件初始化完成")

    def get_qinglong_token(self) -> Optional[str]:
        """获取青龙面板访问令牌"""
        if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
            logger.error("青龙面板配置不完整：地址/Client ID/Client Secret 缺失")
            return None
        
        url = f"{self.ql_panel_url}/open/auth/token?client_id={self.ql_client_id}&client_secret={self.ql_client_secret}"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            token_data = response.json()
            
            if token_data.get("code") == 200 and token_data.get("data", {}).get("token"):
                logger.info("青龙面板访问令牌获取成功")
                return token_data["data"]["token"]
            else:
                error_msg = token_data.get("message", "未知错误")
                logger.error(f"获取青龙令牌失败：{error_msg}，响应数据：{token_data}")
                return None
                
        except requests.exceptions.ConnectionError:
            logger.error(f"获取青龙令牌失败：无法连接到青龙面板地址 {self.ql_panel_url}")
            return None
        except requests.exceptions.Timeout:
            logger.error(f"获取青龙令牌失败：请求超时（{self.ql_panel_url}）")
            return None
        except Exception as e:
            logger.error(f"获取青龙令牌异常：{str(e)}", exc_info=True)
            return None

    def get_all_envs(self, token: str) -> List[Dict]:
        """获取青龙面板所有环境变量（兼容分页/列表格式）"""
        url = f"{self.ql_panel_url}/open/envs"
        headers = {"Authorization": f"Bearer {token}"}
        all_envs = []

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            # 强制将响应转为字符串后解析JSON（解决bytes/str混用问题）
            response_text = response.text.strip()
            env_data = json.loads(response_text)

            # 兼容青龙不同返回格式
            if isinstance(env_data, list):
                all_envs = env_data
                logger.info(f"青龙环境变量：直接获取到 {len(all_envs)} 个变量")
            elif isinstance(env_data, dict):
                if env_data.get("code") == 200:
                    data = env_data.get("data", {})
                    if isinstance(data, dict):
                        all_envs = data.get("items", [])
                    elif isinstance(data, list):
                        all_envs = data
                    logger.info(f"青龙环境变量：分页接口获取到 {len(all_envs)} 个变量")
                else:
                    error_msg = env_data.get("message", "未知错误")
                    logger.error(f"获取青龙环境变量失败：{error_msg}")
            else:
                logger.error(f"青龙环境变量返回格式异常：{type(env_data)}")
                
        except requests.exceptions.ConnectionError:
            logger.error(f"获取青龙环境变量失败：无法连接到 {self.ql_panel_url}")
        except requests.exceptions.Timeout:
            logger.error(f"获取青龙环境变量失败：请求超时")
        except json.JSONDecodeError:
            logger.error(f"青龙环境变量响应解析失败：非JSON格式，响应内容：{response.text[:200]}")
        except Exception as e:
            logger.error(f"获取青龙环境变量异常：{str(e)}", exc_info=True)

        return all_envs

    def count_bili_envs(self, token: str) -> Tuple[int, List[Dict]]:
        """统计B站Cookie环境变量数量（强制刷新）"""
        if not token:
            logger.error("统计B站账号失败：未获取到青龙令牌")
            return 0, []
        
        # 强制重新获取环境变量
        all_envs = self.get_all_envs(token)
        bili_envs = []
        for env in all_envs:
            env_name = env.get("name", b"").decode('utf-8') if isinstance(env.get("name"), bytes) else str(env.get("name", ""))
            if env_name.startswith(CHECK_PREFIX):
                bili_envs.append(env)
        
        # 按后缀数字排序（保证顺序正确）
        def extract_num(name: str) -> int:
            try:
                return int(name.split("__")[-1])
            except (IndexError, ValueError):
                return 99999
        
        bili_envs.sort(key=lambda x: extract_num(str(x["name"])))
        logger.info(f"当前B站账号数量：{len(bili_envs)}/{self.max_account}")
        return len(bili_envs), bili_envs
    
    def generate_qrcode(self) -> Tuple[Optional[str], Optional[str]]:
        """生成B站登录二维码（返回oauth_key和临时文件路径）"""
        try:
            resp = self.session.get(QRCODE_GENERATE_URL)
            resp.raise_for_status()
            data = resp.json()
            
            if data["code"] != 0:
                error_msg = data["message"]
                logger.error(f"生成B站二维码失败：{error_msg}")
                return None, None
            
            qrcode_url = data["data"]["url"]
            oauth_key = data["data"]["qrcode_key"]
            
            # 生成二维码图片并保存到临时文件
            qr = qrcode.QRCode(version=1, box_size=10, border=1)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            # 创建临时文件（自动清理）
            temp_fd, temp_path = tempfile.mkstemp(suffix='.png', prefix='bili_qr_')
            os.close(temp_fd)  # 关闭文件描述符
            img.save(temp_path)
            
            logger.info(f"B站登录二维码生成成功，临时文件路径：{temp_path}")
            return oauth_key, temp_path
            
        except Exception as e:
            logger.error(f"生成二维码异常：{str(e)}", exc_info=True)
            return None, None

    def check_qrcode_status(self, oauth_key: str) -> Optional[Dict]:
        """轮询二维码登录状态"""
        try:
            for _ in range(60):  # 最多轮询2分钟（60*2秒）
                params = {"qrcode_key": oauth_key}
                resp = self.session.get(QRCODE_CHECK_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                
                if data["code"] != 0:
                    error_msg = data["message"]
                    logger.error(f"检查二维码状态失败：{error_msg}")
                    return None
                
                status_code = data["data"]["code"]
                if status_code == 0:
                    logger.info("B站二维码登录成功，开始提取Cookie")
                    cookies = self.get_unique_cookies(self.session.cookies)
                    return self.complement_cookies(cookies)
                elif status_code == 86038:
                    logger.warning("B站二维码已过期")
                    return None
                elif status_code == 86101:
                    logger.debug("等待用户扫描B站二维码...")
                elif status_code == 86090:
                    logger.debug("用户已扫描二维码，等待确认...")
                
                time.sleep(2)
            
            logger.warning("B站二维码登录超时（2分钟）")
            return None
            
        except Exception as e:
            logger.error(f"轮询二维码状态异常：{str(e)}", exc_info=True)
            return None

    def get_unique_cookies(self, cookies) -> Dict:
        """去重Cookie，保留最新值"""
        cookie_dict = {}
        for cookie in cookies:
            cookie_dict[cookie.name] = cookie.value
        return cookie_dict

    def complement_cookies(self, cookies: Dict) -> Dict:
        """访问B站主页补全Cookie"""
        try:
            resp = self.session.get(HOME_PAGE_URL, cookies=cookies)
            if resp.status_code == 200:
                new_cookies = self.get_unique_cookies(resp.cookies)
                cookies.update(new_cookies)
                logger.info("Cookie补全成功，新增字段：{}".format(", ".join(new_cookies.keys())))
            return cookies
        except Exception as e:
            logger.error(f"补全Cookie异常：{str(e)}", exc_info=True)
            return cookies

    def validate_cookie(self, cookies: Dict) -> Tuple[bool, str]:
        """验证Cookie有效性"""
        required_fields = ["DedeUserID", "SESSDATA", "bili_jct"]
        missing = [f for f in required_fields if f not in cookies]
        
        if missing:
            return False, f"缺少必要Cookie字段：{', '.join(missing)}"
        
        if not cookies["DedeUserID"].isdigit():
            return False, "DedeUserID格式无效（非数字）"
        
        if len(cookies["SESSDATA"]) < 20:
            return False, "SESSDATA格式无效（长度不足20）"
        
        if len(cookies["bili_jct"]) != 32:
            return False, "bili_jct格式无效（长度不为32）"
        
        return True, "Cookie验证通过"

    def save_cookie_to_qinglong(self, cookies: Dict, uid: int) -> Tuple[bool, str]:
        """保存Cookie到青龙面板"""
        token = self.get_qinglong_token()
        if not token:
            return False, "获取青龙面板令牌失败"
        
        try:
            # 查询已有环境变量
            url = f"{self.ql_panel_url}/open/envs"
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, params={"searchValue": CHECK_PREFIX}, headers=headers, timeout=10)
            resp.raise_for_status()
            # 强制转为字符串解析JSON
            resp_text = resp.text.strip()
            data = json.loads(resp_text)
            
            if data.get("code") != 200:
                error_msg = data.get("message", "未知错误")
                return False, f"查询青龙环境变量失败：{error_msg}"
            
            env_list = data.get("data", [])
            if isinstance(env_list, dict):
                env_list = env_list.get("items", [])
            
            # 构造Cookie字符串
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            user_id = cookies.get("DedeUserID", str(uid))
            
            # 检查是否已有该用户的Cookie（修复startswith类型问题）
            # 替换原有检查逻辑
            existing_env = None
            for env in env_list:
                # 统一处理字节/字符串类型
                env_name = env.get("name", b"").decode('utf-8') if isinstance(env.get("name"), bytes) else str(env.get("name", ""))
                env_remarks = env.get("remarks", b"").decode('utf-8') if isinstance(env.get("remarks"), bytes) else str(env.get("remarks", ""))
                
                if env_name.startswith(CHECK_PREFIX) and env_remarks == f"bili-{user_id}":
                    existing_env = env
                    break
            
            # 准备环境变量数据
            env_data = {
                "name": existing_env["name"] if existing_env else f"{CHECK_PREFIX}{len(env_list)}",
                "value": cookie_str,
                "remarks": f"bili-{user_id}"
            }
            
            # 新增/更新环境变量
            if existing_env:
                env_data["id"] = existing_env["id"]
                resp = requests.put(f"{self.ql_panel_url}/open/envs", json=env_data, headers=headers, timeout=10)
                action = "更新"
            else:
                resp = requests.post(f"{self.ql_panel_url}/open/envs", json=[env_data], headers=headers, timeout=10)
                action = "新增"
            
            resp.raise_for_status()
            # 解析响应（强制字符串处理）
            result_text = resp.text.strip()
            result = json.loads(result_text)
            
            if result.get("code") == 200:
                logger.info(f"{action}B站Cookie成功：{env_data['name']} (bili-{user_id})")
                return True, f"{action}Cookie成功！UID：{user_id}"
            else:
                error_msg = result.get("message", "未知错误")
                return False, f"{action}Cookie失败：{error_msg}"
                
        except requests.exceptions.ConnectionError:
            return False, "无法连接到青龙面板"
        except requests.exceptions.Timeout:
            return False, "青龙面板请求超时"
        except json.JSONDecodeError:
            return False, f"青龙响应解析失败：非JSON格式"
        except Exception as e:
            logger.error(f"保存Cookie到青龙异常：{str(e)}", exc_info=True)
            return False, f"保存Cookie异常：{str(e)}"

    def delete_bili_cookie(self, token: str, uid: int) -> Tuple[bool, str]:
        """删除指定UID的B站Cookie，并重新整理命名保证连续"""
        if not token:
            return False, "青龙令牌获取失败"
        
        # 1. 获取所有B站相关环境变量
        all_envs = self.get_all_envs(token)
        bili_envs = []
        target_env = None
        target_index = -1
        
        # 筛选B站Cookie并找到目标UID的环境变量
        for idx, env in enumerate(all_envs):
            env_name = env.get("name", b"").decode('utf-8') if isinstance(env.get("name"), bytes) else str(env.get("name", ""))
            env_remarks = env.get("remarks", b"").decode('utf-8') if isinstance(env.get("remarks"), bytes) else str(env.get("remarks", ""))
            
            if env_name.startswith(CHECK_PREFIX):
                bili_envs.append(env)
                # 找到待删除的环境变量
                if env_remarks == f"bili-{uid}":
                    target_env = env
                    target_index = idx
        
        if not target_env:
            return False, f"未找到UID为 {uid} 的B站Cookie"
        
        # 2. 删除目标环境变量
        try:
            url = f"{self.ql_panel_url}/open/envs"
            headers = {"Authorization": f"Bearer {token}"}
            # 执行删除
            resp = requests.delete(url, json=[target_env["id"]], headers=headers, timeout=10)
            resp.raise_for_status()
            
            delete_result = resp.json()
            if delete_result.get("code") != 200:
                error_msg = delete_result.get("message", "未知错误")
                return False, f"删除Cookie失败：{error_msg}"
            
            logger.info(f"成功删除UID {uid} 的Cookie：{target_env['name']}")
            
            # 3. 重新整理剩余B站Cookie的命名（保证连续）
            # 过滤掉已删除的环境变量，重新排序
            remaining_bili_envs = [env for env in bili_envs if env["id"] != target_env["id"]]
            
            # 按原名称后缀数字排序（确保顺序正确）
            def extract_suffix(env):
                name = str(env.get("name", ""))
                try:
                    return int(name.split("__")[-1])
                except (IndexError, ValueError):
                    return 99999
            
            remaining_bili_envs.sort(key=extract_suffix)
            
            # 4. 批量更新环境变量名称
            update_fail_list = []
            for new_suffix, env in enumerate(remaining_bili_envs):
                new_name = f"{CHECK_PREFIX}{new_suffix}"
                old_name = str(env.get("name", ""))
                
                # 名称已正确无需更新
                if old_name == new_name:
                    continue
                
                # 构造更新数据
                update_data = {
                    "id": env["id"],
                    "name": new_name,
                    "value": env["value"],
                    "remarks": env["remarks"]
                }
                
                # 执行更新
                try:
                    update_resp = requests.put(f"{self.ql_panel_url}/open/envs", json=update_data, headers=headers, timeout=10)
                    update_resp.raise_for_status()
                    update_result = update_resp.json()
                    
                    if update_result.get("code") != 200:
                        update_fail_list.append(f"{old_name} → {new_name}（{update_result.get('message')}）")
                    else:
                        logger.info(f"环境变量重命名成功：{old_name} → {new_name}")
                except Exception as e:
                    update_fail_list.append(f"{old_name} → {new_name}（{str(e)}）")
            
            # 5. 处理更新失败的情况
            if update_fail_list:
                fail_msg = "；".join(update_fail_list)
                return True, f"删除成功（UID：{uid}），但部分环境变量重命名失败：{fail_msg}"
            else:
                return True, f"删除成功（UID：{uid}），环境变量已重新整理为连续命名"
                
        except requests.exceptions.ConnectionError:
            return False, "无法连接到青龙面板"
        except requests.exceptions.Timeout:
            return False, "青龙面板请求超时"
        except Exception as e:
            logger.error(f"删除Cookie并整理命名异常：{str(e)}", exc_info=True)
            return False, f"删除/整理异常：{str(e)}"

    @filter.command_group("bilitool", alias={'哔哩哔哩账号管理'})
    def bilitool(self):
        pass
    
    @bilitool.command("info", alias={'介绍'})
    async def info(self, event: AstrMessageEvent):
        """介绍指令（可以查看介绍 使用bilitool info即可）"""
        
        token = self.get_qinglong_token()
        count, _ = self.count_bili_envs(token) if token else (0, [])
        
        # 获取青龙面板中的B站任务配置（新增逻辑）
        config_info = "暂无配置信息（青龙面板连接失败）"
        if token:
            all_envs = self.get_all_envs(token)
            if all_envs:
                # 定义需要展示的配置项映射
                config_mapping = self.ql_env_mapping
                # 遍历获取配置项当前值
                config_lines = []
                for env_name, desc in config_mapping.items():
                    # 查找对应环境变量
                    env_value = "未配置"
                    for env in all_envs:
                        current_name = env.get("name", b"").decode('utf-8') if isinstance(env.get("name"), bytes) else str(env.get("name", ""))
                        if current_name == env_name:
                            env_value = env.get("value", "未配置")
                            break
                    config_lines.append(f"• {desc}：{env_value}")
                config_info = "\n".join(config_lines)
            else:
                config_info = "暂无配置信息（未查询到青龙面板环境变量）"
        info_msg=f"""此插件可以每天增加最多65经验，可以快速升级lv6

目前唯一缺陷是自动看视频会增加一些浏览记录或者点赞，不会影响账号其它东西，具体配置由机器人所有者填写

功能任务说明可查看：
https://github.com/RayWangQvQ/BiliBiliToolPro?tab=readme-ov-file#2-功能任务说明

此工具使用的项目为rayWangQvQ/BiliBiliToolPro，您可以直接在本地/青龙部署此项目

当前存储的账号数量：{count}/{self.max_account}
{config_info}
        """
        yield event.plain_result(info_msg)
        
    @bilitool.command("help", alias={'帮助', 'helpme'})
    async def help(self, event: AstrMessageEvent):
        """帮助指令"""
        # 获取当前账号数量
        token = self.get_qinglong_token()
        count, _ = self.count_bili_envs(token) if token else (0, [])
        
        # 获取青龙面板中的B站任务配置（新增逻辑）
        config_info = "暂无配置信息（青龙面板连接失败）"
        if token:
            all_envs = self.get_all_envs(token)
            if all_envs:
                # 定义需要展示的配置项映射
                config_mapping = self.ql_env_mapping
                # 遍历获取配置项当前值
                config_lines = []
                for env_name, desc in config_mapping.items():
                    # 查找对应环境变量
                    env_value = "未配置"
                    for env in all_envs:
                        current_name = env.get("name", b"").decode('utf-8') if isinstance(env.get("name"), bytes) else str(env.get("name", ""))
                        if current_name == env_name:
                            env_value = env.get("value", "未配置")
                            break
                    config_lines.append(f"• {desc}：{env_value}")
                config_info = "\n".join(config_lines)
            else:
                config_info = "暂无配置信息（未查询到青龙面板环境变量）"
        
        help_msg = f"""风险声明：此工具不能保证安全性，所有者可直接查看ck，可直接控制账号！
此工具引用的开源项目为rayWangQvQ/BiliBiliToolPro，您可以直接在本地/青龙部署此项目

为了保证安全性，此账户在登录和登出都需要扫码验证，以防止任何人都可以删除你的ck
如果不想扫码登出，可以直接将uid告诉所有者让其删除

当前存储的账号数量：{count}/{self.max_account}
{config_info}

注意：尖括号内的值<var>等只需要填写数字
例如 /bilitool login 1057790493

BiliTool 帮助：

 指令列表：
 登录Bili账号 /bilitool login <uid> 
 - 登录会申请一个登录二维码，扫码后请在手机端确认登录，如果提示地点请选择在自己设备登录
 登出Bili账号 /bilitool logout <uid> 
 - 登出会申请一个登录二维码，此次请求仅验证您的身份，如果需要直接删除请联系所有者

所有者指令：
 删除账户 /bilitool forcelogout <uid>  
 直接添加ck /bilitool addck <ck> <uid>
"""
        yield event.plain_result(help_msg)

    @bilitool.command("login", alias={'登录'})
    async def login(self, event: AstrMessageEvent, uid: int):
        """登录指令"""
        qr_temp_path = None  # 临时文件路径，用于最后清理
        try:
            # 1. 基础检查
            if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
                yield event.plain_result("❌ 青龙面板配置不完整，请检查地址/Client ID/Client Secret")
                return
            
            # 2. 获取青龙令牌
            token = self.get_qinglong_token()
            if not token:
                yield event.plain_result("❌ 获取青龙面板访问令牌失败，请检查配置或网络")
                return
            
            # 3. 检查账号数量
            count, _ = self.count_bili_envs(token)
            if count >= self.max_account:
                yield event.plain_result(f"❌ 当前账号数量已达上限：{count}/{self.max_account}，无法添加新账号")
                return
            
            # 4. 测试模式判断（最后判断）
            if self.test:
                yield event.plain_result(f"⚠️ 测试模式已开启，当前账号数量：{count}/{self.max_account}，跳过登录流程")
                return
            
            # 5. 生成二维码（返回临时文件路径）
            yield event.plain_result(f"📱 正在为UID {uid} 生成登录二维码，请稍候...")
            oauth_key, qr_temp_path = self.generate_qrcode()
            
            if not oauth_key or not qr_temp_path:
                yield event.plain_result("❌ 生成二维码失败，请重试")
                return
            
            # 6. 发送本地二维码文件
            yield event.image_result(qr_temp_path)  # 传入本地文件路径
            yield event.plain_result(f"✅ 请使用B站APP扫描上方二维码登录（2分钟内有效）")
            
            # 7. 轮询登录状态
            cookies = self.check_qrcode_status(oauth_key)
            if not cookies:
                yield event.plain_result("❌ 二维码登录失败（超时/过期/取消）")
                return
            
            # 8. 验证Cookie
            valid, msg = self.validate_cookie(cookies)
            if not valid:
                yield event.plain_result(f"❌ Cookie验证失败：{msg}")
                return
            
            # 9. 保存到青龙
            success, msg = self.save_cookie_to_qinglong(cookies, uid)
            if success:
                new_count, _ = self.count_bili_envs(token)
                yield event.plain_result(f"✅ {msg}")
            else:
                yield event.plain_result(f"❌ 保存Cookie失败：{msg}")
        finally:
            # 清理临时文件
            if qr_temp_path and os.path.exists(qr_temp_path):
                try:
                    os.remove(qr_temp_path)
                    logger.info(f"临时二维码文件已清理：{qr_temp_path}")
                except Exception as e:
                    logger.warning(f"清理临时二维码文件失败：{str(e)}")

    @bilitool.command("logout", alias={'删除'})
    async def logout(self, event: AstrMessageEvent, uid: int):
        """登出指令"""
        qr_temp_path = None
        try:
            # 1. 基础检查
            if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
                yield event.plain_result("❌ 青龙面板配置不完整")
                return
            
            # 2. 测试模式判断
            if self.test:
                yield event.plain_result(f"⚠️ 测试模式已开启，跳过登出验证流程（UID：{uid}）")
                return
            
            # 3. 生成验证二维码（临时文件）
            yield event.plain_result(f"📱 请扫码验证身份以删除UID {uid} 的账号（仅验证身份，无实际登录）")
            oauth_key, qr_temp_path = self.generate_qrcode()
            
            if not oauth_key or not qr_temp_path:
                yield event.plain_result("❌ 生成验证二维码失败")
                return
            
            # 4. 发送本地二维码文件
            yield event.image_result(qr_temp_path)
            yield event.plain_result("✅ 请使用B站APP扫描上方二维码验证身份（2分钟内有效）")
            
            # 5. 轮询验证状态
            cookies = self.check_qrcode_status(oauth_key)
            if not cookies:
                yield event.plain_result("❌ 身份验证失败（超时/过期/取消）")
                return
            
            # 6. 验证Cookie中的UID是否匹配
            cookie_uid = cookies.get("DedeUserID")
            if str(cookie_uid) != str(uid):
                yield event.plain_result(f"❌ 身份验证失败：扫码账号UID（{cookie_uid}）与待删除UID（{uid}）不匹配")
                return
            
            # 7. 删除Cookie
            token = self.get_qinglong_token()
            success, msg = self.delete_bili_cookie(token, uid)
            
            if success:
                new_count, _ = self.count_bili_envs(token) if token else (0, [])
                yield event.plain_result(f"✅ {msg}")
            else:
                yield event.plain_result(f"❌ {msg}")
        finally:
            # 清理临时文件
            if qr_temp_path and os.path.exists(qr_temp_path):
                try:
                    os.remove(qr_temp_path)
                    logger.info(f"临时二维码文件已清理：{qr_temp_path}")
                except Exception as e:
                    logger.warning(f"清理临时二维码文件失败：{str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilitool.command("forcelogout", alias={'由所有者直接删除账户'})
    async def forcelogout(self, event: AstrMessageEvent, uid: int):
        """强制删除指令（管理员）"""
        # 1. 基础检查
        if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
            yield event.plain_result("❌ 青龙面板配置不完整")
            return
        
        # 2. 获取令牌并删除
        token = self.get_qinglong_token()
        success, msg = self.delete_bili_cookie(token, uid)
        
        if success:
            new_count, _ = self.count_bili_envs(token) if token else (0, [])
            yield event.plain_result(f"✅ {msg}\n当前账号数量：{new_count}/{self.max_account}")
        else:
            yield event.plain_result(f"❌ {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilitool.command("addck", alias={'由所有者直接添加ck'})
    async def addck(self, event: AstrMessageEvent, ck: str, uid: int):
        """手动添加CK指令（管理员）"""
        # 1. 基础检查
        if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
            yield event.plain_result("❌ 青龙面板配置不完整")
            return
        
        # 2. 解析CK字符串
        cookie_dict = {}
        for item in ck.split(";"):
            item = item.strip()
            if "=" in item:
                key, value = item.split("=", 1)
                cookie_dict[key] = value
        
        # 3. 验证CK
        valid, msg = self.validate_cookie(cookie_dict)
        if not valid:
            yield event.plain_result(f"❌ CK验证失败：{msg}")
            return
        
        # 4. 检查账号数量
        token = self.get_qinglong_token()
        if not token:
            yield event.plain_result("❌ 获取青龙令牌失败")
            return
        
        count, _ = self.count_bili_envs(token)
        if count >= self.max_account:
            yield event.plain_result(f"❌ 账号数量已达上限：{count}/{self.max_account}")
            return
        
        # 5. 保存到青龙
        success, msg = self.save_cookie_to_qinglong(cookie_dict, uid)
        if success:
            new_count, _ = self.count_bili_envs(token)
            yield event.plain_result(f"✅ {msg}\n当前账号数量：{new_count}/{self.max_account}")
        else:
            yield event.plain_result(f"❌ 添加CK失败：{msg}")

    async def terminate(self):
        """插件销毁方法"""
        logger.info("BiliTool插件已销毁")
