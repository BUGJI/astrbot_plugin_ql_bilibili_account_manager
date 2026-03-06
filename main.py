import asyncio
import json
from io import BytesIO
from typing import Dict, List, Tuple, Optional

import re
import os
import tempfile
import httpx
import qrcode

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig

# 常量
QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_CHECK_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
HOME_PAGE_URL = "https://www.bilibili.com/"
CHECK_PREFIX = "Ray_BiliBiliCookies__"

# =========================
# 辅助函数：ql_env_mapping 解析
# =========================
def parse_ql_env_mapping(raw_text: str, strict: bool = True) -> Dict[str, str]:
    """
    解析 ql_env_mapping 文本（每行：描述;变量名）
    返回字典：{ "ENV_VAR_NAME": "显示文本" }
    strict=True 时：遇到非法行会抛出 ValueError 并列出错误行
    """
    mapping = {}
    bad_lines = []
    lines = raw_text.splitlines()
    for idx, line in enumerate(lines, start=1):
        s = line.strip()
        if not s:
            continue
        if ";" not in s:
            bad_lines.append((idx, line))
            continue
        parts = [p.strip() for p in s.split(";", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            bad_lines.append((idx, line))
            continue
        desc, varname = parts[0], parts[1]
        mapping[varname] = desc
    if bad_lines and strict:
        msgs = [f"Line {ln}: {content!r}" for ln, content in bad_lines]
        raise ValueError("ql_env_mapping 格式错误，非法行：" + "; ".join(msgs))
    return mapping

# =========================
# 二维码生成（同步操作放入线程）
# =========================
def _make_qr_bytes_sync(qr_text: str) -> BytesIO:
    qr = qrcode.QRCode(version=1, box_size=10, border=1)
    qr.add_data(qr_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

async def generate_qr_bytes(qr_text: str) -> BytesIO:
    """
    在线程池中执行同步二维码生成，返回 BytesIO（已 seek(0)）。
    使用 asyncio.to_thread 避免阻塞事件循环，且不产生嵌套事件循环问题。
    """
    return await asyncio.to_thread(_make_qr_bytes_sync, qr_text)

def split_log_by_account(log_text: str) -> Dict[int, str]:
    pattern = re.compile(
        r"######### 账号 (\d+) #########([\s\S]*?)(?=######### 账号|\Z)"
    )
    result = {}
    for m in pattern.finditer(log_text):
        idx = int(m.group(1))
        result[idx] = m.group(2).strip()
    return result

# =========================
# Cookie 工具
# =========================
def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    d = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            d[key.strip()] = value.strip()
    return d

def merge_cookies_from_response(resp_cookies) -> Dict[str, str]:
    res = {}
    try:
        # httpx cookies: resp.cookies is Cookies, can iterate
        for c in resp_cookies:
            # c might be cookie tuple or httpx._models.Cookie
            try:
                name = getattr(c, "name", None)
                value = getattr(c, "value", None)
                if name:
                    res[name] = value
                else:
                    # fallback: item might be (k, v)
                    if isinstance(c, tuple) and len(c) >= 2:
                        res[c[0]] = c[1]
            except Exception:
                pass
    except Exception:
        try:
            res.update(dict(resp_cookies))
        except Exception:
            pass # 你有办法吗
    return res

# =========================
# BiliClient: 与 B站交互（异步 httpx）
# =========================
class BiliClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
            }
        )


    async def generate_qrcode(self) -> Tuple[Optional[str], Optional[BytesIO]]:
        try:
            resp = await self.client.get(QRCODE_GENERATE_URL)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"生成二维码接口返回错误：{data}")
                return None, None
            qrcode_url = data["data"]["url"]
            oauth_key = data["data"]["qrcode_key"]
            # 生成二维码 BytesIO（内存）
            img_bytes = await generate_qr_bytes(qrcode_url)
            return oauth_key, img_bytes
        except Exception as e:
            logger.error(f"generate_qrcode 异常：{e}", exc_info=True)
            return None, None

    async def check_qrcode_status(self, oauth_key: str, timeout_seconds: int = 120) -> Optional[Dict]:
        """
        轮询二维码登录状态，使用 asyncio.sleep 避免阻塞。
        成功时返回合并后的 cookie 字典（包含补全后的 cookie）。
        """
        try:
            elapsed = 0
            interval = 2
            while elapsed < timeout_seconds:
                params = {"qrcode_key": oauth_key}
                resp = await self.client.get(QRCODE_CHECK_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                # B 站返回结构复杂：优先检查 data.code 字段
                if data.get("code") != 0:
                    # 当 top-level code 非 0 时，通常是接口错误或提示
                    # 特殊处理：某些 code 代表过期
                    try:
                        inner_code = data.get("data", {}).get("code")
                        if inner_code == 86038:
                            logger.warning("B站二维码已过期")
                            return None
                        # 其他情况继续轮询
                    except Exception:
                        logger.debug(f"二维码检查返回非预期数据：{data}")
                else:
                    status_code = data.get("data", {}).get("code")
                    if status_code == 0:
                        # 登录成功，尝试补全 cookie（请求首页）
                        cookies = {}
                        try:
                            # 合并当前客户端已接收到的 cookies
                            cookies.update({c.name: c.value for c in self.client.cookies.jar})
                        except Exception:
                            # 备用
                            try:
                                cookies.update(dict(self.client.cookies))
                            except Exception:
                                pass
                        cookies = await self.complement_cookies(cookies)
                        logger.info("B站二维码登录成功，已提取并补全 Cookies")
                        return cookies
                    elif status_code == 86038:
                        logger.warning("B站二维码已过期（内部code）")
                        return None
                    # 86101: 等待扫码; 86090: 已扫描等待确认
                await asyncio.sleep(interval)
                elapsed += interval
            logger.warning("二维码轮询超时")
            return None
        except Exception as e:
            logger.error(f"check_qrcode_status 异常：{e}", exc_info=True)
            return None

    async def complement_cookies(self, cookies: Dict) -> Dict:
        """
        访问 B 站首页以补全服务器在 Set-Cookie 中设置的 cookie。
        返回合并后的 cookie dict。
        """
        try:
            resp = await self.client.get(HOME_PAGE_URL, cookies=cookies)
            resp.raise_for_status()
            new_cookies = merge_cookies_from_response(resp.cookies)
            cookies.update(new_cookies)
            logger.debug(f"补全Cookie成功，新增字段：{','.join(new_cookies.keys())}")
            return cookies
        except Exception as e:
            logger.error(f"complement_cookies 异常：{e}", exc_info=True)
            return cookies

    async def validate_cookie(self, cookies: Dict) -> Tuple[bool, str]:
        """
        验证 Cookie 有效性，保持与原函数签名一致。
        """
        required_fields = ["DedeUserID", "SESSDATA", "bili_jct"]
        missing = [f for f in required_fields if f not in cookies]
        if missing:
            return False, f"缺少必要Cookie字段：{', '.join(missing)}"
        if not str(cookies["DedeUserID"]).isdigit():
            return False, "DedeUserID格式无效（非数字）"
        if len(cookies["SESSDATA"]) < 20:
            return False, "SESSDATA格式无效（长度不足20）"
        if len(cookies["bili_jct"]) != 32:
            return False, "bili_jct格式无效（长度不为32）"
        return True, "Cookie验证通过"

    async def close(self):
        await self.client.close()

# =========================
# QinglongClient: 与青龙面板交互（异步 httpx）
# =========================
class QinglongClient:
    def __init__(self, panel_url: str, client_id: str, client_secret: str):
        self.ql_panel_url = panel_url.rstrip("/") if panel_url else ""
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = httpx.AsyncClient(timeout=15.0)

    async def get_token(self) -> Optional[str]: 
        if not all([self.ql_panel_url, self.client_id, self.client_secret]): 
            logger.error("青龙面板配置不完整") 
            return None 
        url = f"{self.ql_panel_url}/open/auth/token?client_id={self.client_id}&client_secret={self.client_secret}" 
        try: 
            resp = await self.client.get(url)
            logger.info(f"get_token 响应状态：{resp.status_code}，内容：{resp.text}")
            resp.raise_for_status() 
            data = resp.json() 
            if data.get("code") == 200 and data.get("data", {}).get("token"): 
                logger.info("青龙面板访问令牌获取成功") 
                return data["data"]["token"] 
            logger.error(f"获取青龙令牌失败：{data}") 
            return None 
        except Exception as e:
            logger.error(f"获取青龙令牌异常：{e}", exc_info=True) 
            return None



    async def get_all_envs(self, token: str) -> List[Dict]:
        url = f"{self.ql_panel_url}/open/envs"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self.client.get(url, headers=headers)
            resp.raise_for_status()
            text = resp.text
            data = json.loads(text)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and data.get("code") == 200:
                d = data.get("data", {})
                if isinstance(d, dict):
                    return d.get("items", [])
                if isinstance(d, list):
                    return d
            return []
        except Exception as e:
            logger.error(f"获取青龙环境变量异常：{e}", exc_info=True)
            return []

    async def save_cookie_to_qinglong(self, cookies: Dict, uid: int) -> Tuple[bool, str]:
        token = await self.get_token()
        if not token:
            return False, "获取青龙面板令牌失败"
        try:
            url = f"{self.ql_panel_url}/open/envs"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            resp = await self.client.get(url, params={"searchValue": CHECK_PREFIX}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 200:
                return False, f"查询青龙环境变量失败：{data.get('message', '')}"
            env_list = data.get("data", [])
            if isinstance(env_list, dict):
                env_list = env_list.get("items", [])

            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            user_id = cookies.get("DedeUserID", str(uid))

            existing_env = None
            for env in env_list:
                name = env.get("name", "")
                if isinstance(name, bytes):
                    try:
                        name = name.decode("utf-8", errors="ignore")
                    except Exception:
                        name = str(name)
                remarks = env.get("remarks", "")
                if name.startswith(CHECK_PREFIX) and remarks == f"bili-{user_id}":
                    existing_env = env
                    break
            
            # 在这里再次提及插件设计寿命极短
            # 变量名本来就必须连续，如果不连续则会导致处理任务的程序无法正常运行，你并不能依赖这个插件去修复这个错误
            # 我能做的就是尽力去保证每一次正常运行的时候不会出现意外错误
            if existing_env:
                env_data = {"id": existing_env["id"], "name": existing_env["name"], "value": cookie_str, "remarks": f"bili-{user_id}"}
                up = await self.client.put(url, json=env_data, headers=headers)
                up.raise_for_status()
                result = up.json()
                if result.get("code") == 200:
                    logger.info(f"更新B站Cookie成功：{existing_env['name']}")
                    return True, f"更新Cookie成功！UID：{user_id}"
                else:
                    return False, f"更新Cookie失败：{result.get('message')}"
            else:
                new_name = f"{CHECK_PREFIX}{len(env_list)}"
                env_payload = [{"name": new_name, "value": cookie_str, "remarks": f"bili-{user_id}"}]
                post = await self.client.post(url, json=env_payload, headers=headers)
                post.raise_for_status()
                result = post.json()
                if result.get("code") == 200:
                    logger.info(f"新增B站Cookie成功：{new_name}")
                    return True, f"新增Cookie成功！UID：{user_id}"
                else:
                    return False, f"新增Cookie失败：{result.get('message')}"
        except Exception as e:
            logger.error(f"保存Cookie到青龙异常：{e}", exc_info=True)
            return False, f"保存Cookie异常：{e}"

    async def delete_bili_cookie(self, token: str, uid: int) -> Tuple[bool, str]:
        """使用尾部覆盖方式安全删除指定UID的B站Cookie"""

        if not token:
            return False, "青龙令牌获取失败"

        try:
            # 复用客户端会导致神秘崩溃，请图灵辟邪之前请不要动这坨代码
            async with httpx.AsyncClient(timeout=10) as client:

                # 1. 获取全部 Cookie 环境变量
                url = f"{self.ql_panel_url}/open/envs"
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params={"searchValue": CHECK_PREFIX})
                resp.raise_for_status()
                all_envs = resp.json().get("data", [])

                # 排序，确保 bili_cookie__0 1 2 ... 顺序一致
                def extract_suffix(env):
                    try:
                        return int(str(env["name"]).split("__")[-1])
                    except:
                        return 99999

                bili_envs = sorted(
                    [env for env in all_envs if str(env.get("name", "")).startswith(CHECK_PREFIX)],
                    key=extract_suffix
                )

                # 找到目标 cookie
                target_env = None
                for env in bili_envs:
                    if str(env.get("remarks", "")) == f"bili-{uid}":
                        target_env = env
                        break

                if not target_env:
                    return False, f"未找到UID {uid} 的Cookie"

                # 如果只有 1 条，直接删即可
                if len(bili_envs) == 1:
                    delete_resp = await client.request(
                        "DELETE",
                        f"{self.ql_panel_url}/open/envs?id=",
                        json=[target_env["id"]],
                        headers={"Authorization": f"Bearer {token}"}
                    )
                    delete_resp.raise_for_status()
                    return True, f"删除成功（UID：{uid}）"

                # 2. 获取最后一条
                last_env = bili_envs[-1]

                # 3. 如果要删除的不是最后一个 → 则用最后一个覆盖它
                if target_env["id"] != last_env["id"]:
                    update_data = {
                        "id": target_env["id"],
                        "name": target_env["name"],  # 名称保持不变！
                        "value": last_env["value"],
                        "remarks": last_env["remarks"]
                    }

                    put_resp = await client.put(
                        f"{self.ql_panel_url}/open/envs",
                        json=update_data,
                        headers={"Authorization": f"Bearer {token}"}
                    )
                    put_resp.raise_for_status()

                # 4. 删除最后一条
                delete_resp = await client.request(
                    "DELETE",
                    f"{self.ql_panel_url}/open/envs?id=",
                    json=[last_env["id"]],
                    headers={"Authorization": f"Bearer {token}"}
                )
                delete_resp.raise_for_status()
                
                # 你不应该在这里
                # user_key = str(event.get_sender_id())
                # self.user_cookie_index.pop(user_key, None)
                # self._save_user_index()

                return True, f"删除成功（UID：{uid}）"
                


        except httpx.ConnectError:
            return False, "无法连接到青龙面板"
        except httpx.TimeoutException:
            return False, "青龙面板请求超时"
        except Exception as e:
            logger.error(f"删除Cookie异常：{str(e)}", exc_info=True)
            return False, f"删除Cookie异常：{str(e)}"
        
    async def get_crons(self, token: str) -> List[Dict]:
        url = f"{self.ql_panel_url}/open/crons"
        resp = await self.client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        data = resp.json().get("data", {})
        
        # data 可能是 dict，里面有 data 列表
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        elif isinstance(data, list):
            return data
        else:
            return []


    async def get_cron_logs(self, token: str, cron_id: int) -> str:
        url = f"{self.ql_panel_url}/open/crons/{cron_id}/logs"
        resp = await self.client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.text

    async def get_cron_log_content(self, token: str, task_id: int, filename: str) -> str:
        url = f"{self.ql_panel_url}/open/crons/log"  # 确保 base url 末尾不要多余字符
        headers = {"Authorization": f"Bearer {token}"}

        filename = filename.strip()

        logger.info(f"Fetching log: taskId={task_id}, filename={filename}")
        logger.info(f"URL: {url}")
        logger.info(f"Headers: {headers}")
        
        params = {"taskId": task_id, "filename": filename}  # taskId 注意大小写
        resp = await self.client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = await resp.json()
        return data["data"]["content"]


    
    async def close(self):
        await self.client.close()

# =========================
# 插件主类（保持 MyPlugin 名称与方法签名）
# =========================
@register("astrbot_plugin_ql_bilibili_account_manager", "BUGJI", "将账号扫码登录到青龙的Bili任务执行器，需要青龙面板且安装BiliToolPro，不会配置可以看仓库", "v0.1.14514")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 请不要肘击这里的代码，这些都设置了默认值
        self.name = "astrbot_plugin_ql_bilibili_account_manager"
        self.config = config
        self.ql_panel_url = self.config.ql_config.get("ql_panel_url", "").rstrip("/")
        self.ql_client_id = self.config.ql_config.get("ql_client_id", "")
        self.ql_client_secret = self.config.ql_config.get("ql_client_secret", "")
        raw_mapping = self.config.slot_config.get("ql_env_mapping", "")
        
        self.user_cookie_index: Dict[str, int] = {}
        self.index_file = os.path.join(
            get_astrbot_data_path(),
            "plugin_data",
            self.name,
            "bili_user_index.json"
        )
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)

        try:
            # 你选择了严格模式（非法行会报错），这里保持 strict=True
            self.ql_env_mapping = parse_ql_env_mapping(raw_mapping, strict=True)
        except ValueError as e:
            logger.error(f"解析 ql_env_mapping 失败：{e}")
            self.ql_env_mapping = {}

        self.max_account = int(self.config.slot_config.get("max_account", 10))
        self.logout_verify = bool(self.config.slot_config.get("logout_verify", True))
        self.test = bool(self.config.slot_config.get("test", False))

        # 业务客户端
        self.bili = BiliClient()
        self.ql = QinglongClient(self.ql_panel_url, self.ql_client_id, self.ql_client_secret)

        

        logger.info(f"BiliTool插件初始化完成，配置：青龙地址={self.ql_panel_url}，最大账号数={self.max_account}，测试模式={self.test}")

    async def initialize(self):
        logger.info("BiliTool插件异步初始化完成")
        
    def _load_user_index(self):
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    self.user_cookie_index = json.load(f)
            except Exception:
                self.user_cookie_index = {}
        else:
            self.user_cookie_index = {}


    def _save_user_index(self):
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(
                self.user_cookie_index,
                f,
                ensure_ascii=False,
                indent=2
            )



    @filter.command_group("bilitool", alias={'哔哩哔哩账号管理'})
    def bilitool(self):
        pass

    @bilitool.command("info", alias={'介绍'})
    async def info(self, event: AstrMessageEvent):
        token = await self.ql.get_token()
        count, _ = await self.count_bili_envs(token) if token else (0, [])

        config_info = "暂无配置信息（青龙面板连接失败）"
        if token:
            all_envs = await self.ql.get_all_envs(token)
            if all_envs:
                lines = []
                for env_name, desc in self.ql_env_mapping.items():
                    value = "未配置"
                    for env in all_envs:
                        current_name = env.get("name", "")
                        if isinstance(current_name, bytes):
                            try:
                                current_name = current_name.decode("utf-8", errors="ignore")
                            except Exception:
                                current_name = str(current_name)
                        if current_name == env_name:
                            value = env.get("value", "未配置")
                            break
                    lines.append(f"• {desc}：{value}")
                config_info = "\n".join(lines)
            else:
                config_info = "暂无配置信息（未查询到青龙面板环境变量）"

        info_msg = f"""此插件可以每天增加最多65经验，可以快速升级lv6

目前唯一缺陷是自动看视频会增加一些浏览记录或者点赞，不会影响账号其它东西，具体配置由机器人所有者填写

此工具使用的项目为rayWangQvQ/BiliBiliToolPro，您可以直接在本地/青龙部署此项目

当前存储的账号数量：{count}/{self.max_account}
{config_info}
        """
        yield event.plain_result(info_msg)

    @bilitool.command("help", alias={'帮助', 'helpme'})
    async def help(self, event: AstrMessageEvent):
        token = await self.ql.get_token()
        count, _ = await self.count_bili_envs(token) if token else (0, [])

        config_info = "暂无配置信息（青龙面板连接失败）"
        if token:
            all_envs = await self.ql.get_all_envs(token)
            if all_envs:
                lines = []
                for env_name, desc in self.ql_env_mapping.items():
                    value = "未配置"
                    for env in all_envs:
                        current_name = env.get("name", "")
                        if isinstance(current_name, bytes):
                            try:
                                current_name = current_name.decode("utf-8", errors="ignore")
                            except Exception:
                                current_name = str(current_name)
                        if current_name == env_name:
                            value = env.get("value", "未配置")
                            break
                    lines.append(f"• {desc}：{value}")
                config_info = "\n".join(lines)
            else:
                config_info = "暂无配置信息（未查询到青龙面板环境变量）"

        help_msg = f"""风险声明：此工具不能保证安全性，所有者可直接查看ck，可直接控制账号！
此工具引用的开源项目为rayWangQvQ/BiliBiliToolPro，您可以直接在本地/青龙部署此项目

当前存储的账号数量：{count}/{self.max_account}
{config_info}

注意：尖括号内的值<uid>只需要替换为数字即可
例如 /bilitool login <uid>
可以填写为 /bilitool login 1057790493

BiliTool 帮助：

 指令列表：
 登录Bili账号 /bilitool login <uid> 
 - 登录会申请一个登录二维码，扫码后请在手机端确认登录，如果提示地点请选择在自己设备登录
 登出Bili账号 /bilitool logout <uid> 
 查询Bili账号状态 /bilitool find <uid>

所有者指令：
 删除账户 /bilitool forcelogout <uid>  
"""
        yield event.plain_result(help_msg)

    @bilitool.command("cx", alias={"查询", "status"})
    async def cx(self, event: AstrMessageEvent):
        return 
        self._load_user_index()
        user_key = str(event.get_sender_id())
        logger.info(f"cx 调用 user_key:{user_key}")
        logger.info(f"当前 user_cookie_index:{self.user_cookie_index}")
        
        slot = self.user_cookie_index.get(user_key)

        if slot is None:
            yield event.plain_result("❌ 未找到你的账号索引，请先登录 /bilitool login")
            return

        token = await self.ql.get_token()
        if not token:
            yield event.plain_result("❌ 青龙面板连接失败")
            return

        # logger.info(f"获取token{token}")
        
        crons = await self.ql.get_crons(token)
        target = None
        for c in crons:
            if not isinstance(c, dict):
                continue
            # logger.info(f"任务名称: {c.get('name')}, 命令: {c.get('command')}")
            if c.get("name") == "bili每日任务" or "bili_task_daily.sh" in str(c.get("command", "")):
                target = c
                break

        if not target:
            yield event.plain_result("❌ 未找到 Bili 定时任务")
            return

        # target_slot = self.user_cookie_index[user_key]
        # logger.info(f"user_key={user_key}, slot={target_slot}")

        # 获取最新日志
        # 获取日志文件列表
        log_text = await self.ql.get_cron_logs(token, target["id"])
        logger.info(f"原始日志返回内容：{log_text[:1000]}")

        try:
            log_json = json.loads(log_text)
            log_list = log_json.get("data", [])
        except Exception:
            log_list = []

        if not log_list:
            yield event.plain_result("⚠️ 该任务没有日志文件")
            return

        # 取最新日志文件
        latest_log = log_list[0]
        filename = latest_log["filename"]

        # 调用青龙单日志接口获取文本内容
        log_content = await self.ql.get_cron_log_content(token, target["id"], filename)
        if not log_content:
            yield event.plain_result("⚠️ 无法获取日志内容")
            return

        # 分账号
        blocks = split_log_by_account(log_content)
        slot = str(self.user_cookie_index[user_key])
        my_log = blocks.get(slot)
        if not my_log:
            yield event.plain_result("⚠️ 找到任务，但本次运行中没有你的账号日志")
            return

        # 显示日志（可截取最后30行）
        lines = my_log.splitlines()
        preview = "\n".join(lines[-30:])
        yield event.plain_result(f"📊 账号 {slot} 最近一次运行日志（截取）：\n\n{preview}")


    @bilitool.command("login", alias={'登录'})
    async def login(self, event: AstrMessageEvent, uid: int):
        qr_stream: Optional[BytesIO] = None
        try:
            if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
                yield event.plain_result("❌ 青龙面板配置不完整，请检查地址/Client ID/Client Secret")
                return

            token = await self.ql.get_token()
            if not token:
                yield event.plain_result("❌ 获取青龙面板访问令牌失败，请检查配置或网络")
                return

            count, _ = await self.count_bili_envs(token)
            if count >= self.max_account:
                yield event.plain_result(f"❌ 当前账号数量已达上限：{count}/{self.max_account}，无法添加新账号")
                return

            # 放在此处主要是可以验证上方的配置和流程是否正确
            if self.test:
                yield event.plain_result(f"⚠️ 测试模式开启，跳出二维码登录流程，无法登录")
                return

            yield event.plain_result(f"📱 正在为UID {uid} 生成登录二维码，请稍候...")
            oauth_key, qr_stream = await self.bili.generate_qrcode()
            if not oauth_key or not qr_stream:
                yield event.plain_result("❌ 生成二维码失败，请重试")
                return

            data = qr_stream.getvalue()
            tmp_path = None # 代码作用域锁定

            # 写入临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            # 用文件路径发送图片
            yield event.image_result(tmp_path)
            if tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)
            
            yield event.plain_result(f"✅ 请使用B站APP扫描上方二维码登录（2分钟内有效）")

            cookies = await self.bili.check_qrcode_status(oauth_key)
            if not cookies:
                yield event.plain_result("❌ 二维码登录失败（超时/过期/取消）")
                return

            valid, msg = await self.bili.validate_cookie(cookies)
            if not valid:
                yield event.plain_result(f"❌ Cookie验证失败：{msg}")
                return

            cookie_uid = cookies.get("DedeUserID")
            if str(cookie_uid) != str(uid):
                yield event.plain_result(f"❌ 身份验证失败：扫码账号UID（{cookie_uid}）与待删除UID（{uid}）不匹配")
                return
            
            success, msg = await self.ql.save_cookie_to_qinglong(cookies, uid)
            if success:
                # ===== 新增：建立 AstrBot 用户 → Cookie 槽位索引 =====
                token = await self.ql.get_token()
                _, envs = await self.count_bili_envs(token)

                # 找刚刚这个 uid 对应的 env
                slot_index = None
                for env in envs:
                    if str(env.get("remarks")) == f"bili-{uid}":
                        try:
                            slot_index = int(str(env["name"]).split("__")[-1])
                        except Exception:
                            pass
                        break

                if slot_index is not None:
                    user_key = str(event.get_sender_id())
                    self.user_cookie_index[user_key] = slot_index
                    self._save_user_index()
                # ===== 新增结束 =====

                new_count, _ = await self.count_bili_envs(token)
                yield event.plain_result(f"✅ {msg}")
            else:
                yield event.plain_result(f"❌ 保存Cookie失败：{msg}")
        finally:
            if qr_stream:
                try:
                    qr_stream.close()
                except Exception:
                    pass
    @bilitool.command("find", alias={'查询账户', '查找'})
    async def find_account(self, event: AstrMessageEvent, uid: int):
        """
        通过UID查询账户是否存在于青龙面板中
        """
        try:
            # 获取青龙令牌
            token = await self.ql.get_token()
            if not token:
                yield event.plain_result("❌ 青龙面板连接失败，无法查询")
                return

            # 获取所有B站Cookie环境变量
            _, bili_envs = await self.count_bili_envs(token)
            
            if not bili_envs:
                yield event.plain_result("📊 当前青龙面板中没有存储任何B站账号")
                return

            # 查找指定UID的账户
            target_env = None
            account_info = []
            
            for env in bili_envs:
                remarks = env.get("remarks", "")
                name = env.get("name", "")
                
                # 检查备注是否匹配
                if remarks == f"bili-{uid}":
                    target_env = env
                    
                    # 从Cookie值中提取更多信息
                    cookie_value = env.get("value", "")
                    cookie_dict = parse_cookie_string(cookie_value)
                    
                    # 获取用户名（如果有的话，B站的Cookie中通常不直接包含用户名）
                    # 可以从其他字段获取，但这里只显示基础信息
                    account_info.append({
                        "name": name,
                        "remarks": remarks,
                        "cookie_keys": list(cookie_dict.keys()),
                        "cookie_count": len(cookie_dict)
                    })
                    break

            if target_env:
                # 构建详细信息
                info = target_env
                cookie_value = info.get("value", "")
                
                # 解析Cookie（只显示关键字段，不显示完整值保护隐私）
                cookie_dict = parse_cookie_string(cookie_value)
                masked_cookie = {}
                for key in ["DedeUserID", "SESSDATA", "bili_jct"]:
                    if key in cookie_dict:
                        value = cookie_dict[key]
                        # 对敏感信息进行脱敏处理
                        if key == "DedeUserID":
                            masked_cookie[key] = value  # UID可以不脱敏
                        else:
                            masked_cookie[key] = value[:6] + "****" + value[-4:] if len(value) > 10 else "****"
                
                # 获取环境变量创建/更新时间（如果青龙API返回这些信息）
                created_at = info.get("created_at", "未知")
                updated_at = info.get("updated_at", "未知")
                
                result_msg = f"""✅ 找到UID {uid} 的账户
    • 创建时间：{created_at}
    • 更新时间：{updated_at}
    • 状态：{"已启用" if info.get("status") == 0 else "已禁用"}
    """
                
                # 获取该账户在列表中的索引位置
                for idx, env in enumerate(bili_envs):
                    if env.get("id") == target_env.get("id"):
                        result_msg += f"• 索引： {idx + 1}/{len(bili_envs)} 个"
                        break
                        
                yield event.plain_result(result_msg)
            else:
                # 没找到指定UID，显示所有UID列表
                all_uids = []
                for env in bili_envs:
                    remarks = env.get("remarks", "")
                    if remarks.startswith("bili-"):
                        uid_str = remarks.replace("bili-", "")
                        if uid_str.isdigit():
                            all_uids.append(int(uid_str))
                
                if all_uids:
                    uid_list = ", ".join([str(uid) for uid in sorted(all_uids)])
                    yield event.plain_result(f"❌ 未找到UID {uid} 的账户")
                else:
                    yield event.plain_result(f"❌ 未找到UID {uid} 的账户")
                    
        except Exception as e:
            logger.error(f"查询账户时发生异常：{e}", exc_info=True)
            yield event.plain_result(f"❌ 查询过程中发生错误：{str(e)}")
        

    @bilitool.command("logout", alias={'删除'})
    async def logout(self, event: AstrMessageEvent, uid: int):
        qr_stream: Optional[BytesIO] = None
        try:
            if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
                yield event.plain_result("❌ 青龙面板配置不完整")
                return

            if self.logout_verify:
                if self.test:
                    yield event.plain_result(f"⚠️ 测试模式开启，跳出二维码验证，删除失败")
                    return

                yield event.plain_result(f"📱 请扫码验证身份以删除UID {uid} 的账号（仅验证身份，无实际登录）")
                oauth_key, qr_stream = await self.bili.generate_qrcode()
                if not oauth_key or not qr_stream:
                    yield event.plain_result("❌ 生成验证二维码失败")
                    return

                data = qr_stream.getvalue()
                tmp_path = None # 代码作用域锁定

                # 写入临时文件
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name

                # 用文件路径发送图片
                yield event.image_result(tmp_path)
                if tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)
                yield event.plain_result("✅ 请使用B站APP扫描上方二维码验证身份（2分钟内有效）")
                
                cookies = await self.bili.check_qrcode_status(oauth_key)
                if not cookies:
                    yield event.plain_result("❌ 身份验证失败（超时/过期/取消）")
                    return

                cookie_uid = cookies.get("DedeUserID")
                if str(cookie_uid) != str(uid):
                    yield event.plain_result(f"❌ 身份验证失败：扫码账号UID（{cookie_uid}）与待删除UID（{uid}）不匹配")
                    return
            else:
                yield event.plain_result(f"开始删除UID {uid} 的账号")

            token = await self.ql.get_token()
            success, msg = await self.ql.delete_bili_cookie(token, uid)
            
            user_key = str(event.get_sender_id())
            if user_key in self.user_cookie_index:
                self.user_cookie_index.pop(user_key)
                self._save_user_index()
            
            if success:
                new_count, _ = await self.count_bili_envs(token) if token else (0, [])
                yield event.plain_result(f"✅ {msg}")
            else:
                yield event.plain_result(f"❌ {msg}")
        finally:
            if qr_stream:
                try:
                    qr_stream.close()
                except Exception:
                    pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilitool.command("forcelogout", alias={'由所有者直接删除账户'})
    async def forcelogout(self, event: AstrMessageEvent, uid: int):
        if not all([self.ql_panel_url, self.ql_client_id, self.ql_client_secret]):
            yield event.plain_result("❌ 青龙面板配置不完整")
            return

        token = await self.ql.get_token()
        success, msg = await self.ql.delete_bili_cookie(token, uid)
        
        user_key = str(event.get_sender_id())
        if user_key in self.user_cookie_index:
            self.user_cookie_index.pop(user_key)
            self._save_user_index()
        
        if success:
            new_count, _ = await self.count_bili_envs(token) if token else (0, [])
            yield event.plain_result(f"✅ {msg}\n当前账号数量：{new_count}/{self.max_account}")
        else:
            yield event.plain_result(f"❌ {msg}")

    async def count_bili_envs(self, token: str) -> Tuple[int, List[Dict]]:
        if not token:
            logger.error("统计B站账号失败：未获取到青龙令牌")
            return 0, []

        all_envs = await self.ql.get_all_envs(token)
        bili_envs = []
        for env in all_envs:
            name = env.get("name", "")
            if isinstance(name, bytes):
                try:
                    name = name.decode("utf-8", errors="ignore")
                except Exception:
                    name = str(name)
            if name.startswith(CHECK_PREFIX):
                bili_envs.append(env)

        def extract_num(name: str) -> int:
            try:
                return int(name.split("__")[-1])
            except Exception:
                return 99999

        bili_envs.sort(key=lambda x: extract_num(str(x.get("name", ""))))
        logger.info(f"当前B站账号数量：{len(bili_envs)}/{self.max_account}")
        return len(bili_envs), bili_envs

    async def terminate(self):
        # 关闭异步客户端
        try:
            await self.bili.close()
        except Exception:
            logger.error(f"哔哩登录模块未正常关闭")
            pass
        try:
            await self.ql.close()
        except Exception:
            logger.error(f"青龙模块未正常关闭")
            pass
        logger.info("BiliTool插件已销毁")
