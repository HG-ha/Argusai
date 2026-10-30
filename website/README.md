# ArgusAI website

`website/` 是 `argusai.sh` 的静态官网源码，与桌面端 `argusai/webui` 独立。

## 本地预览

```sh
python -m http.server 4173 --directory website
```

然后访问 `http://127.0.0.1:4173`。

## Cloudflare Pages

- Framework preset: `None`
- Build command: 留空
- Build output directory: `website`
- Root directory: `/`

Pages 会直接发布 `website/`。自定义域名使用 `argusai.sh`，可将 `www.argusai.sh` 重定向到根域名。

官网的产品能力描述来自仓库 `README.md`，真实界面截图放在 `assets/screens/`。
