# 力竭了 
# 留给后人赤石这一块

import asyncio
import json
from io import BytesIO
from typing import Dict, List, Tuple, Optional

import os
import tempfile
import httpx
import qrcode

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
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
            pass
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
        await self.client.aclose()

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
            logger.error("青龙面板配置不完整：地址/Client ID/Client Secret 缺失")
            return None
        
        # 这里没有体面的方法了，只能拼接URL参数
        url = f"{self.ql_panel_url}/open/auth/token?client_id={self.client_id}&client_secret={self.client_secret}"
        try:
            resp = await self.client.get(url)
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

                return True, f"删除成功（UID：{uid}）"

        except httpx.ConnectError:
            return False, "无法连接到青龙面板"
        except httpx.TimeoutException:
            return False, "青龙面板请求超时"
        except Exception as e:
            logger.error(f"删除Cookie异常：{str(e)}", exc_info=True)
            return False, f"删除Cookie异常：{str(e)}"


# =========================
# 插件主类（保持 MyPlugin 名称与方法签名）
# =========================
@register("astrbot_plugin_ql_bilibili_account_manager", "BUGJI", "将账号扫码登录到青龙的Bili任务执行器，需要青龙面板且安装BiliToolPro，不会配置可以看仓库", "v0.1.14514")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.ql_panel_url = self.config.ql_config.get("ql_panel_url", "").rstrip("/")
        self.ql_client_id = self.config.ql_config.get("ql_client_id", "")
        self.ql_client_secret = self.config.ql_config.get("ql_client_secret", "")
        raw_mapping = self.config.slot_config.get("ql_env_mapping", "")
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

所有者指令：
 删除账户 /bilitool forcelogout <uid>  
"""
        yield event.plain_result(help_msg)

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

            # 写入临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            # 用文件路径发送图片
            yield event.image_result(tmp_path)

            yield event.plain_result(f"✅ 请使用B站APP扫描上方二维码登录（2分钟内有效）")
            
            os.remove(tmp_path)

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
                # 写入临时文件
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name

                # 用文件路径发送图片
                yield event.image_result(tmp_path)

                yield event.plain_result("✅ 请使用B站APP扫描上方二维码验证身份（2分钟内有效）")
                
                os.remove(tmp_path)
                
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
            pass
        try:
            await self.ql.close()
        except Exception:
            pass
        logger.info("BiliTool插件已销毁")
