# 介绍

适用于 AstrBot 的 Bilibili 账号自动化程序，基于[青龙面板](https://github.com/whyour/qinglong)为载体 [BiliBiliToolPro](https://github.com/RayWangQvQ/BiliBiliToolPro/)为实际任务

此插件仅扫码登录和向添加cookie操作，没有其它任务，插件无实际负载

可以每天给登录的bili账号获取最多65经验，每日自动领取大会员权益等，请查看引用项目的BiliTool的[功能任务说明](https://github.com/RayWangQvQ/BiliBiliToolPro?tab=readme-ov-file#2-)的任务范围

此插件需要青龙面板作为前置科技，并且已部署rayWangQvQ/BiliBiliToolPro作为自动化程序，部署需要的项目请查看[部署](#部署)


# 风险声明

<b>此工具不能保证安全性，所有者可直接查看登录的cookie，这些理论可直接控制账号！</b>

如果您是用户，确保仅使用您熟悉的人搭建的此项目使用，否则可能导致账号被盗的情况

如果您是所有者，请妥善保管并且给青龙面板设置强密码，获取青龙面板权限等同于拿到此网络下的js、py、sh执行权限

# 部署

1.首先部署 [青龙面板](https://github.com/whyour/qinglong) 面板，如果你有NAS可以去应用商店安装或者部署Docker版

> 这边优先推荐使用NAS部署，可以长期稳定运行

2.在安装完成后，通过[BiliTool的青龙部署运行](https://github.com/RayWangQvQ/BiliBiliToolPro/blob/main/qinglong/README.md)将此项目的脚本拉取在青龙面板中

2.1 在青龙面板中的 系统设置>应用设置>创建应用

任意起名，权限勾选 环境变量

复制Cilent ID和Cilent Secret备用

2.2 配置基本环境变量

| 需要添加的环境变量 | 功能 | 建议值 |
| ----  | ---- | ---- |
| Ray_DailyTaskConfig__NumberOfProtectedCoins|哔哩哔哩最少保留的硬币数量|建议为10
| DailyTaskConfig__SaveCoinsWhenLv6|哔哩哔哩在lv6之后每日白嫖硬币|可以设置为true
| DailyTaskConfig__IsShareVideo|哔哩哔哩分享视频（不实际分享给任何人 可+5经验）|分享多了可能会风控，建议为false
| DailyTaskConfig__SelectLike|哔哩哔哩点赞（可能会增加推荐关联性）|建议为false
| Ray_DailyTaskConfig__SupportUpIds|优先投币支持的UP主UID（可能会增加推荐关联性 -1关闭）|建议为-1，可以使用英文逗号分隔多个UID
在bilitool info和help会展示青龙环境变量映射配置，增加变量映射配置 注解;变量名 一行一个

这些环境变量添加后会在菜单展示，以便用户更好知道操作了账号的什么东东，如果不想展示这些请接着往下看

3.安装此插件，然后在插件配置中，配置你的 面板IP 以及上方的 Cilent ID 和 Cilent Secret

| 配置 | 介绍 | 建议 |
| ----- | ---- | ---- |
| 测试模式 | 仅用于控制是否生成二维码登录 | 开启后可以测试环境 |
| 登出扫码验证 | 用于防止登录的账户被他人删除的情况 | 可信环境可关闭 |
| 最大登录的账户个数 | 太大可能导致自己家宽风控 | 建议不变/更低值 |
| 青龙环境变量映射 | 展示已经配置的环境值 | 建议按上面的添加面板配置，增加配置按照格式即可 |

# 使用

| 命令 | 介绍 |
| ----  | ---- |
| bilitool | 基本命令，目前没写 可以展示命令树 |
| bilitool login <uid> | 用于登录账号，填写UID以校验，防止其他人扫码登录 |
| bilitool logout <uid> | 用于登出账号，填写UID以登出 |
| bilitool forcelogout <uid> | bot所有者可绕过扫码登出此账号 |

# 其它

感谢RaywangQVQ、whyour大叠造福B友的项目

感谢豆包和GPT-5帮我写这坨代码
