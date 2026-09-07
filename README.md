# boss-zhipin-automation

BOSS直聘（BOSS zhipin，国内求职平台）求职沟通自动化工具集：进件监控、消息收发、岗位挖掘、联系记录管理。纯 HTTP / MQTT 通道发消息，不用浏览器自动化操作发送，内置风控保护与礼貌边界。

> An open-source toolkit automating job-chat workflows on BOSS zhipin (a Chinese job platform): inbound monitoring, messaging, job prospecting, contact management. Pure HTTP/MQTT transport, no browser automation for sending.

**仅供学习研究 / For learning and research only.** 请阅读文末免责声明。

## 功能一览

- **进件监控**（heartbeat）：轮询 friend list，检测「招聘方最后发言」的变化，产出结构化事件 JSON；pending/ack 机制保证 cron 崩溃不丢消息；无事件静默退出。
- **消息发送**（boss-send）：MQTT over WebSocket + protobuf，绕开页面自动化；45 秒强制冷却、daemon 单连接复用、发送后落库验证。
- **岗位挖掘**（prospecting）：按城市/关键词搜索岗位，自动去重（已会话 / 已打招呼 / 非目标区县），输出候选清单。
- **联系记录**（contact-log）：本地状态机记录每家公司的触达状态（greeted / no_follow / protected），避免重复骚扰，婉拒后不再触达。
- **会话与职位读取**（msg-tools）：只读 HTTP 接口，查会话历史、职位详情。
- **CDP 辅助**（cdp-helper / reply）：浏览器级 cookie 读取与页面只读观察，用于登录态恢复，不做页面点击发送。

## 架构

```
┌─────────────┐   friend list HTTP    ┌──────────────────┐
│  heartbeat   │ ───────────────────▶ │  events JSON      │──▶ cron agent 回复
└─────────────┘                       └──────────────────┘
┌─────────────┐   MQTT over WSS       ┌──────────────────┐
│  boss-send   │ ───────────────────▶ │  BOSS 服务器      │
│ (45s 冷却)   │ ◀─────────────────── │  (protobuf)      │
└─────────────┘   daemon socket 复用   └──────────────────┘
┌─────────────┐
│  prospecting │──▶ 岗位搜索 ──▶ 去重 ──▶ 打招呼候选
│  contact-log │──▶ no_follow 公司自动跳过
└─────────────┘
```

| 模块 | 用途 | 通道 |
|---|---|---|
| `boss-message-heartbeat.py` | 进件监控（cron 驱动） | 纯 HTTP，只读 |
| `boss-mqtt/boss-send.py` | **发消息唯一通道** | MQTT over WebSocket + protobuf |
| `boss-mqtt/boss-mqtt-daemon.py` | 常驻 MQTT 连接守护 | MQTT 长连接 |
| `boss-mqtt/boss-listen.py` | 消息监听 | MQTT |
| `boss-mqtt/boss-msg-tools.py` | 会话历史 / 职位详情 | 纯 HTTP，只读 |
| `boss-mqtt/boss-contact-log.py` | 公司联系状态机 | 本地 JSON |
| `boss-mqtt/boss-prospecting.py` | 主动找岗位 + 打招呼 | 纯 HTTP |
| `boss-reply.py` | CLI 桥接辅助 | boss-agent-cli |
| `boss-cdp-helper.py` | CDP websocket 工具 | 本地 9222，只读 |
| `start-boss-chrome-cdp.sh` | 启动带调试口的 Chrome profile | 本地 |

## 风控设计（本项目核心价值）

这些规则全部来自真实使用中的踩坑（翻车记录写在注释里）：

- **发消息只走 MQTT**，页面自动化只读。页面点击发送实测触发风控（刷新循环 + 登录态塌陷）。
- **45 秒最小发送间隔**：`.last_send_ts` 强制冷却，实测连续 5 条后被踢。
- **国内站必须直连**：请求前清空代理环境变量并显式 `ProxyHandler({})`。代理 IP 与 cookie 不匹配返回 code 7/37。
- **单 MQTT 连接**：存在 daemon socket 自动复用，绝不并行第二条（双连接互踢）。
- **UA 固定** Chrome/151，所有脚本一致，不要换。
- **IP 被封 = 会话全灭**：服务端作废该账号全部会话，本地 cookie 全失效，换 IP 无效，只能重新扫码。
- **pending/ack**：事件未确认送达前不 ack，下轮重复出现，防 cron 崩溃丢消息。

## 快速开始

```bash
git clone https://github.com/attnk2-oss/boss-zhipin-automation.git
cd boss-zhipin-automation
pip install websocket-client   # MQTT/CDP 依赖

# 1. 写个人配置（不入库，路径见 boss_config.py）
mkdir -p ~/.config/boss-zhipin
cat > ~/.config/boss-zhipin/config.json <<'EOF'
{
  "my_uid": 12345678,
  "city_code": "101010100",
  "preferred_districts": ["你的目标区县"],
  "state_path": "~/.hermes/scripts/boss-zhipin-state.json"
}
EOF

# 2. 准备登录态（cookies JSON，从已登录浏览器导出，见下节）
# 3. 跑监控
python3 boss-message-heartbeat.py
```

## 凭证与状态（全部不入库）

运行时需要的外部文件，已被 `.gitignore` 全部拦截：

```
<state_path>                        # cookies 数组（boss_config 配置）
~/.hermes/cache/boss-contact-log.json      # 联系记录数据
~/.hermes/cache/boss-message-heartbeat.json# heartbeat 状态
~/.boss-agent/auth/session.enc             # 可选：boss-agent-cli 独立加密凭证
```

登录态获取：登录 BOSS直聘后从浏览器 DevTools 导出 zhipin.com 域的 cookies（关键值 `wt2` 与 `__zp_stoken__`），按 `{"cookies": [...]}` 格式存入 state 文件。仓库不包含、也永远不会包含任何真实凭证。

## 用法示例

```bash
# 进件监控（建议 cron 每 3 分钟；无事件静默）
python3 boss-message-heartbeat.py

# 回复并验证送达后 ack
python3 boss-message-heartbeat.py --ack <uid>

# 发消息（唯一合法发送通道）
python3 boss-mqtt/boss-send.py "消息文本" --uid <uid> --encrypt-uid <encryptUid>

# 按姓名+公司定位发送
python3 boss-mqtt/boss-send.py "消息文本" --name "联系人" --brand "公司名"

# 会话历史 / 职位详情
python3 boss-mqtt/boss-msg-tools.py history --uid <uid> --limit 30
python3 boss-mqtt/boss-msg-tools.py job --uid <uid>

# 公司触达状态（no_follow 不再触达）
python3 boss-mqtt/boss-contact-log.py check --company "公司名"

# 岗位挖掘（城市与区县来自个人配置）
python3 boss-mqtt/boss-prospecting.py --query "电商运营"
```

## 目录结构

```
boss-zhipin-automation/
├── boss_config.py            # 运行时配置读取（环境变量 / ~/.config/boss-zhipin/config.json）
├── boss-message-heartbeat.py # 进件监控
├── boss-reply.py             # CLI 桥接辅助
├── boss-cdp-helper.py        # CDP 工具（只读）
├── start-boss-chrome-cdp.sh  # CDP Chrome 启动脚本
└── boss-mqtt/
    ├── boss-send.py          # 发消息（MQTT）
    ├── boss-mqtt-daemon.py   # 常驻连接守护
    ├── boss-listen.py        # 消息监听
    ├── boss-msg-tools.py     # 会话/职位只读
    ├── boss-contact-log.py   # 联系状态机
    └── boss-prospecting.py   # 岗位挖掘
```

## 免责声明 / Disclaimer

- 本项目**仅供学习研究**个人自动化技术（MQTT 协议、protobuf 编解码、风控对抗设计、cron 任务可靠性），请勿用于任何违反 BOSS直聘用户协议或当地法律法规的用途。
- 自动化操作社交/求职平台存在账号风控、封禁风险，使用本项目产生的一切后果由使用者自行承担。
- 请尊重平台与其他用户：不要群发垃圾消息，不要绕过频控，婉拒后请停止触达（项目内置的 no_follow 机制就是这个目的）。
- 本项目与 BOSS直聘官方无关。
- **For learning and research only.** Automating third-party platforms may violate their terms of service; use at your own risk. Respect rate limits and other users.

## License

MIT
