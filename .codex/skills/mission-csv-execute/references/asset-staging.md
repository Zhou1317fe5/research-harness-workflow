# 资产 staging：本地取回 → 上传远端 (Asset Staging)

输入资产（数据集、权重、checkpoint）与评测产出工件（`evidence/*.npz`、预测掩码）是两类东西，
规则相反，别混：

- **产出工件**：留在远端，本地只拉 KB 摘要；`review.md`/`handoff.md` 引用 `<host>:<absolute_path>`，严禁拉回、严禁反向 scp。
- **输入资产**：远端网络经常比本地慢。当远端直连下载很慢时，正确做法就是 **本地快速下载 → 上传到远端资产目录**。

## 1. 何时走本地取回再上传

1. 先在远端测速（例如 `ssh <host> "curl -o /dev/null -s -w '%{speed_download}\n' <url>"`）；
2. 远端明显低于本地（如 < 1 MB/s 而本地可达数十 MB/s）→ 本地下载再上传；
3. 远端已存在且大小一致 → 直接复用，不重复传输。

## 2. 铁律：非必要不做大文件哈希

数据集图像、掩码、样本与 GB 级权重/checkpoint **一律不做全量 sha256**（本地算一遍、远端再算一遍等于白耗 I/O 数分钟）。
完整性以 **文件大小（st_size）** 为准：上传后远端 `stat -c %s` 回读与本地大小比对即可。
只有小文件或用户明确要求时才用 `--sha256` 校验。这条与「废除数据级加密证据链与数据全量哈希」一致。

## 3. 标准工具：stage_asset.py

```bash
# 本地下载（可断点）后再上传到远端资产目录
python3 .agents/harness/remote/stage_asset.py \
  --profile adam \
  --remote-path /home/zhoujiale/cdfss-dinov3-workspace/weights/xxx.pth \
  --local /tmp/xxx.pth \
  --url https://.../xxx.pth

# 本地已有文件，直接上传
python3 .agents/harness/remote/stage_asset.py \
  --profile eve --remote-path /srv/.../data.tgz --local /tmp/data.tgz
```

行为要点：
- 复用 profiles.json 的 SSH 连接（含连接复用参数），流式传输（**rsync `--partial --inplace` 断点续传**；
  rsync 不可用或密码型 profile 时回退 `scp -C`），**绝不把大文件读进内存**；
- 幂等：远端已存在且大小一致即跳过；
- 默认不哈希，按大小校验；`--sha256` 时才校验；`--force` 强制重传。

## 4. 资产落盘登记与复用

- 上传目标应落在该项目的稳定资产目录（如 `weights/…`、`datasets/…`），并登记到 `project.toml`/`HERA_*_ROOT`
  等配置，让后续 RunSpec 直接引用该路径，避免每次重新下载/上传。
- 多台机器共用资产时，优先各自 staging 一次并复用，不做跨机反复搬运。
