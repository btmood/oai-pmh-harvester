# 本题的本地 OAI-PMH 数据源

本目录是试标题的公开输入材料；`mock_oai.py` 和内置记录均为本地构造的测试数据。

## Windows 环境

使用 Python 3.13 或以上，在仓库根目录运行 `./trial/setup.ps1`。脚本在仓库的同级目录建立 `.venv`，按 `requirements-tested.txt` 安装本题已实测的依赖版本，然后以 editable 方式安装项目。

在 PowerShell 设置本终端的环境并激活：

```powershell
$env:PYTHONUTF8 = '1'
$env:SENTRY_DSN = 'none'
$env:WORKSPACE = 'trial'
& ../.venv/Scripts/Activate.ps1
python -m pytest -q
```

UTF-8 模式用于兼容原测试中按默认编码读取 UTF-8 文件的行为。已有 40 项测试在该模式下通过。

## 数据源

在项目环境中启动：

```powershell
python trial/mock_oai.py --port 8765 --page-size 3
```

服务只监听 `127.0.0.1`。`GET /health` 返回健康状态，`GET /stats` 返回本次服务收到的请求。重启服务会清除请求计数和故障状态。

| 路径 | 行为 |
| --- | --- |
| `/oai` | 正常分页，9 条记录，其中 1 条是删除标记。 |
| `/oai/fail-once` | 第一次翻到第二页时注入一次 OAI `badArgument` 错误，之后正常；用于先失败再恢复。 |
| `/oai/expire-once` | 第一次翻到第二页时使旧分页 token 失效；重新发起原始查询后签发新一代 token。 |
| `/oai/empty` | 对 ListRecords 返回 `noRecordsMatch`。 |

支持 `Identify`、`ListMetadataFormats`、`ListSets`、`ListRecords`、`ListIdentifiers` 和 `GetRecord` 的本题所需部分。日期过滤按 UTC 日期闭区间，唯一集合为 `trial:fixtures`，metadataPrefix 为 `oai_dc`。数据集在服务存活期间保持不变。

token 是不透明的续页参数；带 token 的请求只允许 verb 和 resumptionToken。故障模式通过路径区分，故障仅发生一次，服务重启后会再次触发。

旧版命令的正常采集示例：

```powershell
oai -h http://127.0.0.1:8765/oai -o sample.xml harvest --method list
```

正式任务要求通过模型会话中的完整 Prompt 传达。故障源按约定返回失败是预期行为。其用途是检查采集程序如何保存原状态、恢复以及发布最终产物。
