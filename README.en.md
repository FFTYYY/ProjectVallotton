<p align="center">
  <img src="docs/assets/logo.png" width="120" alt="Vallotton">
</p>

<h1 align="center">Vallotton</h1>
<p align="center">An AI drawing workflow built on NovelAI V5</p>
<p align="center"><a href="README.md">中文</a> · English</p>

Vallotton is a conversational tool for getting consistent pictures out of NovelAI. You talk to it in plain language, the way you would brief an illustrator; Vallotton plans the composition and turns it into NovelAI V5 prompts.

Underneath is a workflow that helps the drawing AI remember characters, outfits and styles, so that the details of a character do not drift and the style does not wander from one picture to the next. It also edits a single picture, reflects on its own output and repaints, generates a series in one go, and keeps your work in projects.

Full documentation: **https://fftyyy.github.io/ProjectVallotton/en/**

- [Overview](https://fftyyy.github.io/ProjectVallotton/en/index.html)
- [Quick start](https://fftyyy.github.io/ProjectVallotton/en/quickstart.html)
- [User guide](https://fftyyy.github.io/ProjectVallotton/en/guide.html)
- [Deployment](https://fftyyy.github.io/ProjectVallotton/en/deploy.html)

## Quick start

You need:
* a NovelAI persistent token
* a DeepSeek API key

**Windows:**
```bat
start.bat --lang en
```

**macOS / Linux:**

```sh
chmod +x start.sh
./start.sh --lang en
```

The first run asks for the keys and a login, then opens your browser. Without `--lang en` the terminal speaks Chinese; the web app's language is switched inside the app. The [quick start](https://fftyyy.github.io/ProjectVallotton/en/quickstart.html) walks through every step.

## Deploying to a server

Once deployed, you can use Vallotton from anywhere, including your phone, through your own domain. You need a Linux server reachable from the internet, a domain name pointing at it, Docker, nginx and certbot installed on the server, and ports 22, 80 and 443 open in your cloud provider's console.

Download the code, copy the `.env` template and fill in the keys, the login and the domain, then start it:

```sh
sudo mkdir -p /srv/vallotton/media && sudo chown -R $USER /srv/vallotton
git clone https://github.com/FFTYYY/ProjectVallotton.git /srv/vallotton/app
cd /srv/vallotton/app
cp .env.example .env && chmod 600 .env && nano .env
docker compose up -d --build
```

Then put your domain into the nginx template in place of `YOUR_DOMAIN`, enable it, and get a certificate with certbot (replace `vallotton.example.com` below with your domain). After that you log in at `https://your-domain`.

```sh
sudo sh -c 'sed "s/YOUR_DOMAIN/vallotton.example.com/g" deploy/host-nginx.conf.example > /etc/nginx/sites-available/vallotton'
sudo ln -s /etc/nginx/sites-available/vallotton /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d vallotton.example.com
```

Every step in detail, day-to-day maintenance and the environment variables are in the [deployment guide](https://fftyyy.github.io/ProjectVallotton/en/deploy.html).

## License

Released under the [Apache-2.0 license](LICENSE).

Vallotton does not provide the drawing or language-model services itself. The accounts and costs of those third-party services, and the copyright and compliance of what is generated, are the user's own responsibility.
