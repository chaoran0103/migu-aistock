# 觅股：线上部署说明

本包包含最新完整应用代码、上海背景原图、样式、真实历史预览与测试脚本。没有本机绝对路径依赖，不需要本地数据库、BaoStock、历史 CSV/Parquet 或额外前端构建。应用运行时通过 HTTP 查询免费行情与财报源。

## 方式一：Streamlit Community Cloud

1. 解压，把文件放在自己的 GitHub 仓库根目录，保留 `assets/` 与隐藏目录 `.streamlit/`。入口应为仓库根目录的 `app.py`，不要漏传 `conversation.py`、`screening_jobs.py`、`market_preview.py`。
2. 打开 <https://share.streamlit.io/>，新建应用，选择仓库与分支，入口填 `app.py`，Python 选择 **3.12**。
3. 在高级设置或应用设置的 **Secrets** 中粘贴 `.streamlit/secrets.example.toml` 内容，把 `LLM_API_KEY` 的空字符串换成自己的 DeepSeek Key。模型名沿用当前项目配置 `deepseek-flash`；如账号支持的模型名不同，修改 `LLM_MODEL`。不要把实际 Key 提交到 GitHub。
4. 点击部署。部署完成后，在应用分享/访问设置中允许公开访问，复制平台分配的 HTTPS 地址。
5. 用浏览器无痕窗口打开该地址，确认无需你的账号登录即可访问。工作台统一使用对话选股；结果洞察可立即看到历史预览。发送“银行股，PE 低于 10”，在对话内核对条件卡，可点击修改或回复自然语言调整，再点击卡片中的执行按钮。

未填写 Key 时仍可用规则解析；填写后，访客的模型请求由服务端调用并使用该 Key 的额度。访客看不到 Key，也无需填写自己的 Key。页面只显示模型名与连接配置状态。

官方入口：[部署文档](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app)、[应用分享文档](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)。平台界面和免费额度以平台当时说明为准。

## 方式二：Docker / 自有云服务器

需要服务器已安装 Docker；在解压目录执行：

```bash
cp .env.example .env
# 用服务器编辑器在 .env 填写自己的 LLM_API_KEY。
docker build -t migu-stock-ai:latest .
docker run -d --name migu-stock-ai --restart unless-stopped \
  --env-file .env -p 8501:8501 migu-stock-ai:latest
```

默认监听 `0.0.0.0:8501`，可通过服务器公网地址的 8501 端口访问；开放该端口需在服务器防火墙/安全组中配置。正式域名通过云平台或反向代理配置 HTTPS，并支持 Streamlit 的 WebSocket 连接。Docker 型托管平台可直接使用本包 `Dockerfile`，环境变量中填三个 `LLM_*` 配置；平台提供 `PORT` 时启动命令会自动采用该端口。

镜像以非 root 用户运行，内置 `/_stcore/health` 健康检查。真实 `.env`、`.streamlit/secrets.toml` 不会进入镜像。没有持久化数据卷要求。

```bash
docker logs --tail 100 migu-stock-ai
curl -f http://127.0.0.1:8501/_stcore/health
```

本交付环境没有 Docker，Dockerfile 已检查但未实际构建；发布前在目标服务器执行上述构建与健康检查。

## 数据与运行范围

- 市场行情概览是代码内真实历史预览，页面显示具体日期；它不参与个人筛选。
- 点击“执行筛选”才在线请求股票池、快照、财报及需要的日线。切换应用内页面不会中断筛选；对话内有旋转圆圈与每秒更新的耗时。
- 免费数据源在云端可能限流或不可达。东财财报不可达会明确降级；若新浪/腾讯无法提供基础股票池与行情，会显示失败，不生成虚构结果。
- 任务和结果在单个进程内存中；平台休眠、重启或浏览器会话重建可能丢失任务。先按单实例部署；不含跨实例任务队列、持久化或自动扩容协调。
- 当前版本没有站点级访问限流或费用上限；公开访客会共享部署者的模型额度和免费源带宽。可在模型服务商后台设置额度，并按实际访问量配置托管平台的入口限流。
- 仅供研究学习，不构成投资建议；免费数据可能延迟或缺失。

## 发布前检查

```bash
python -m pip install -r requirements.txt
python -m pip check
python selftest_input_flow.py
streamlit run app.py --server.address=0.0.0.0 --server.port=8501
```

`selftest_input_flow.py` 使用隔离测试样本，不调用真实行情或消耗模型额度；`selftest_data.py` 和 `selftest_e2e.py` 则需要目标机器能访问数据源。包内 `MANIFEST.json` 记录文件 SHA-256，可复核解压完整性。`requirements-tested.txt` 记录本次本地验证的六个直接依赖版本，默认部署仍使用 `requirements.txt` 的版本约束。
