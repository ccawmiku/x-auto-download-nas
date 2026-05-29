# X Auto Downloader NAS

基于 `Playwright + yt-dlp` 的 X 点赞媒体自动下载器，目标是在 NAS Docker 上低频运行。

## 生成 Docker 镜像

仓库包含 GitHub Actions：

```text
.github/workflows/docker-image.yml
```

把代码 push 到 `main` 后，进入：

```text
GitHub 仓库 -> Actions -> Docker Image CI
```

等待任务成功，镜像会发布到：

```text
ghcr.io/ccawmiku/x-auto-download-nas:1.0.0
```

这里不用 `latest`，compose 也固定使用 `1.0.0`。以后升级时，把 workflow 和 compose 里的版本号一起改成 `1.0.1`、`1.0.2` 这类版本。

如果 GHCR package 是 private，NAS 拉镜像前需要登录：

```bash
docker login ghcr.io -u ccawmiku
```

密码使用 GitHub Personal Access Token，不是 GitHub 登录密码。

## NAS 部署

先创建目录：

```bash
mkdir -p /volume2/docker/x-auto-download/config
mkdir -p /volume2/docker/x-auto-download/state
mkdir -p /volume2/docker/x-auto-download/downloads-metadata
mkdir -p /volume2/docker/x-auto-download/images
mkdir -p /volume2/docker/x-auto-download/videos
mkdir -p /volume2/docker/x-auto-download-app
```

把 `docker-compose.yml` 放到：

```text
/volume2/docker/x-auto-download-app/docker-compose.yml
```

启动：

```bash
cd /volume2/docker/x-auto-download-app
docker compose pull
docker compose up -d
```

网页端：

```text
http://NAS_IP:13003
```

## 下载目录结构

`docker-compose.yml` 默认挂载：

```yaml
volumes:
  - /volume2/docker/x-auto-download/config:/config
  - /volume2/docker/x-auto-download/state:/state
  - /volume2/docker/x-auto-download/downloads-metadata:/downloads
  - /volume2/docker/x-auto-download/images:/downloads/images
  - /volume2/docker/x-auto-download/videos:/downloads/videos
```

容器内部结构：

```text
/downloads/images      图片，以及 X 动图转出来的 GIF，全部平铺
/downloads/videos      普通视频，全部平铺
/downloads/_metadata   yt-dlp info.json 等元数据
/downloads/_thumbnails 视频缩略图
/downloads/_browser    最近一次 likes 页面截图
/downloads/_tmp        下载临时目录
```

如果你想把图片和视频放到不同位置，只改这两行：

```yaml
  - /volume2/你的图片目录:/downloads/images
  - /volume2/你的视频目录:/downloads/videos
```

## Cookie

推荐从电脑浏览器导出 Netscape 格式 `cookies.txt`，然后在网页端粘贴保存。

保存后容器内路径：

```text
/config/x_cookies.txt
```

也可以直接放到：

```text
/volume2/docker/x-auto-download/config/x_cookies.txt
```

## 停止标记

默认停止标记：

```text
https://x.com/deskt3d/status/1992264334853165368?s=20
```

第一次运行会一直滚动 likes 页面，直到找到这个推文；找到后只下载它上方的新点赞内容，不下载标记本身。

## 功能

- 网页端管理配置和 cookie。
- 用无头浏览器打开 X likes 页面并滚动收集推文链接。
- SQLite 记录已发现和已下载推文，避免重复下载。
- 图片优先尝试高质量 CDN 候选。
- 视频使用 `yt-dlp -f "bv*+ba/b"` 下载最高可用版本。
- X 动图先用 `yt-dlp` 获取最高 mp4，再默认转成 GIF，按图片归类。
- 默认 12 小时运行一次，单线程、随机滚动等待、无并发。
- 下载是严格一条推文一条推文顺序执行，不会并发下载。
- 停止标记只比较 tweet 数字 ID；`x.com`/`twitter.com`、用户名、`?s=20` 这类参数不同都不影响。
