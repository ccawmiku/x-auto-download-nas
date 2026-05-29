# X Auto Downloader NAS

基于 `Playwright + yt-dlp` 的 X 点赞媒体自动下载器，目标是在 NAS Docker 上低频运行。

## 功能

- 网页端管理配置和 cookie。
- 用无头浏览器打开 X likes 页面并滚动收集推文链接。
- 遇到停止标记推文后停止继续向下采集。
- SQLite 记录已发现和已下载推文，避免重复下载。
- 图片优先尝试高质量 CDN 候选：
  - `{media_id}.png?name=4096x4096`
  - `{media_id}.jpg?name=4096x4096`
  - `?format=jpg&name=orig`
- 视频和 X 动图使用 `yt-dlp -f "bv*+ba/b"` 下载最高可用版本。
- 默认 12 小时运行一次，单线程、随机滚动等待、无并发。

## NAS 部署

先在 NAS 上创建目录：

```bash
mkdir -p /volume1/docker/x-auto-download/config
mkdir -p /volume1/docker/x-auto-download/state
mkdir -p /volume1/docker/x-auto-download/downloads
```

复制本仓库到 NAS 后运行：

```bash
docker compose up -d --build
```

网页端默认端口：

```text
http://NAS_IP:13003
```

`docker-compose.yml` 使用了 `command: >`，适配不支持复杂数组 command 的 NAS 面板。

## Cookie

推荐从电脑浏览器导出 Netscape 格式 `cookies.txt`，然后在网页端粘贴保存。

保存后容器内路径是：

```text
/config/x_cookies.txt
```

也可以直接把文件放到 NAS：

```text
/volume1/docker/x-auto-download/config/x_cookies.txt
```

## 停止标记

默认停止标记是：

```text
https://x.com/deskt3d/status/1992264334853165368?s=20
```

第一次运行会一直滚动 likes 页面，直到找到这个推文；找到后只下载它上方的新点赞内容，不下载标记本身。

如果标记不存在或页面无法继续加载，程序会在连续多次没有新增内容后停止，避免无限循环。

## 账号风险控制

这个项目不调用点赞、关注、发帖等修改账号状态的操作，只读取你自己的网页 likes 页面。

默认行为：

- 12 小时运行一次；
- 单浏览器、单线程；
- 滚动距离随机；
- 每次滚动后随机等待；
- 每隔一段滚动随机长暂停；
- 下载任务不并发；
- 失败不会疯狂重试。

## 本地测试

```bash
python -m pip install -r requirements.txt
python x_auto_worker.py --config config/config.json
```

浏览器打开：

```text
http://127.0.0.1:8080
```

手动运行一次：

```bash
python x_auto_worker.py --config config/config.json --run-once
```

## 目录

```text
/config   配置和 cookie
/state    SQLite 数据库
/downloads 下载结果
```

下载文件按作者和 tweet id 分目录保存。
