<p align="center">
  <img src="docs/assets/logo.png" width="120" alt="Vallotton">
</p>

<h1 align="center">Vallotton</h1>
<p align="center">基于NovelAI V5的AI绘图工作流</p>

Vallotton是一个帮你在NovelAI里稳定出图的对话式工具。你可以像和画师沟通一样直接用自然语言交流，由Vallotton负责构图规划并自动转化为NovelAI V5提示词。

Vallotton用一套精巧的工作流，帮助绘图AI记住角色、服装和画风，尝试解决多图生成时角色细节难以控制或样式失控等问题。同时支持单张微调、自动反思修正、连续批量生成以及项目管理等。

完整文档：**https://fftyyy.github.io/ProjectVallotton/**

- [前言](https://fftyyy.github.io/ProjectVallotton/index.html)
- [快速上手](https://fftyyy.github.io/ProjectVallotton/quickstart.html)
- [进阶指南](https://fftyyy.github.io/ProjectVallotton/guide.html)
- [部署教程](https://fftyyy.github.io/ProjectVallotton/deploy.html)

## 快速启动

使用前需准备：
* NovelAI Persistent Token
* DeepSeek API Key

**Windows：**
```bat
start.bat

```

**macOS / Linux：**

```sh
chmod +x start.sh
./start.sh

```

首次运行会提示输入密钥与登录信息，完成后将自动打开浏览器。详细步骤请参考[快速上手](https://fftyyy.github.io/ProjectVallotton/quickstart.html)。

## 服务器部署

部署到服务器以后，可以在任何地方（包括手机上）通过自己的域名使用Vallotton。你需要一台能从公网访问的Linux服务器、一个指向它的域名，服务器上装好Docker、nginx和certbot，并在云服务商的控制台放行22、80、443端口。

下载代码，从模板复制一份`.env`并填上密钥、账号、域名等，然后启动：

```sh
sudo mkdir -p /srv/vallotton/media && sudo chown -R $USER /srv/vallotton
git clone https://github.com/FFTYYY/ProjectVallotton.git /srv/vallotton/app
cd /srv/vallotton/app
cp .env.example .env && chmod 600 .env && nano .env
docker compose up -d --build
```

再把nginx配置模板里的`YOUR_DOMAIN`换成你的域名并启用，用certbot申请证书（下面的`vallotton.example.com`换成你的域名），之后就可以通过`https://你的域名`登录了。

```sh
sudo sh -c 'sed "s/YOUR_DOMAIN/vallotton.example.com/g" deploy/host-nginx.conf.example > /etc/nginx/sites-available/vallotton'
sudo ln -s /etc/nginx/sites-available/vallotton /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d vallotton.example.com
```

每一步的详细说明、日常维护等见[部署教程](https://fftyyy.github.io/ProjectVallotton/deploy.html)。

## 开源协议

本项目基于[Apache-2.0协议](LICENSE)开源。

Vallotton本身不提供绘画与大模型服务。这些第三方服务的账号、费用，以及生成内容的版权和合规，均由使用者自行负责。
