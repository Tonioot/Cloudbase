// Guide content. Each guide: slug, group, title, summary (index card), optional
// lead (intro paragraph, HTML) and body (HTML). h2 headings form the
// "On this page" list automatically.

export const GUIDE_GROUPS = ['Getting started', 'Apps', 'Networking', 'Nodes', 'Administration', 'Reference'];

export const GUIDES = [

/* ─── Getting started ──────────────────────────────────────────────────── */

{
  slug: 'how-it-works',
  group: 'Getting started',
  title: 'How Cloudbase works',
  summary: 'The primary, nodes, apps and instances — and how a request reaches your code.',
  keywords: 'architecture overview concepts primary node instance tunnel',
  lead: 'Cloudbase turns a Git repository into running containers on one or more of your own servers, and puts nginx in front of them. This page explains the four building blocks you’ll see everywhere in the panel.',
  body: `
<h2>The four building blocks</h2>
<table>
  <tr><th>Concept</th><th>What it is</th></tr>
  <tr><td>Primary</td><td>The server where this panel runs. It stores all configuration in its database, builds nothing it doesn’t need to, runs nginx, and is the single entry point for traffic and for you.</td></tr>
  <tr><td>Node</td><td>Any server that runs app containers. The primary is always a node itself (shown as <code>primary</code>). Extra servers join as remote nodes and are controlled by an agent.</td></tr>
  <tr><td>App</td><td>A definition: repository, build and start command, port, environment variables, domains, resource limits. An app doesn’t run by itself.</td></tr>
  <tr><td>Instance</td><td>One running copy of an app — one Docker container on one node. An app with three instances runs three identical containers, possibly on different servers.</td></tr>
</table>

<h2>From repository to container</h2>
<p>When you create an app, Cloudbase clones the repository to <code>~/.cloudbase/apps/&lt;name&gt;</code> on the primary and builds a Docker image from it. If the repository contains its own <code>Dockerfile</code>, that is used as-is. Otherwise Cloudbase writes one for you based on the runtime it detects from your start command (Node.js, Python, Ruby, Go, PHP or a generic Ubuntu image). Each instance is then started from that image.</p>
<p>Instances on remote nodes are built on that node: the agent downloads the source from the primary and builds the same image locally, so nodes don’t need access to your Git host.</p>

<h2>How a request reaches your app</h2>
<div class="flow">
  <span class="flow-box">Visitor<small>myapp.example.com</small></span><span class="flow-arrow">→</span>
  <span class="flow-box">nginx on the primary<small>TLS, load balancing</small></span><span class="flow-arrow">→</span>
  <span class="flow-box">Instance on the primary<small>127.0.0.1:10001</small></span>
</div>
<div class="flow">
  <span class="flow-box">nginx on the primary</span><span class="flow-arrow">→</span>
  <span class="flow-box">Tunnel port<small>127.0.0.1:20000</small></span><span class="flow-arrow">→</span>
  <span class="flow-box">Agent on a node<small>outbound WebSocket</small></span><span class="flow-arrow">→</span>
  <span class="flow-box">Instance on that node</span>
</div>
<p>All public traffic enters through nginx on the primary. nginx spreads requests over every healthy instance of the app. Instances on the primary are reached directly on their host port; instances on other nodes are reached through a <strong>tunnel</strong> the node’s agent opens to the primary. Because the agent connects outwards, remote nodes don’t need any open inbound ports.</p>

<h2>What runs where</h2>
<ul>
  <li><strong>Cloudbase itself</strong> listens on port <code>7823</code>. With a panel domain configured, nginx serves it on 80/443.</li>
  <li><strong>Instances</strong> get a host port from the instance range (default <code>10000–19999</code>).</li>
  <li><strong>Tunnels</strong> use local ports on the primary from the tunnel range (default <code>20000–29999</code>), bound only to <code>127.0.0.1</code>.</li>
  <li><strong>Data</strong> — database, config, certificates, app sources — lives in <code>~/.cloudbase/</code> of the user Cloudbase runs as.</li>
</ul>

<div class="callout callout--tip"><p>The rest of the guides follow the order you’ll usually need them: securing the setup, a first app, then domains, more servers and team access.</p></div>
`,
},

{
  slug: 'running-cloudbase',
  group: 'Getting started',
  title: 'Running Cloudbase',
  summary: 'A checklist for a fresh setup, updates, the service and the admin password.',
  keywords: 'setup checklist secure firewall https update systemd service password uninstall autostart',
  lead: 'You’re in the panel, so Cloudbase is installed. This page covers what’s worth doing right after that, and how to keep the installation healthy over time.',
  body: `
<h2>Checklist for a new setup</h2>
<ol class="guide-steps">
  <li><strong>Make ports 80 and 443 reachable.</strong> All web traffic and HTTPS certificates go through them. Open them in the server’s firewall and your hosting provider’s firewall. Server at home? Forward both ports on your router to the server’s local IP address — the domain wizard shows which one.</li>
  <li><strong>Start on boot.</strong> Run <code>cloudbase enable</code> on the server once, so Cloudbase and its apps come back after a reboot. <code>cloudbase status</code> shows whether the service is installed and running.</li>
  <li><strong>Serve the panel over HTTPS.</strong> Point a domain at the server, enter it under <a href="/settings?s=domain">Domain &amp; SSL</a> and click <strong>Set up HTTPS</strong> — the certificate is free and renews itself. Until then the panel is plain HTTP on port 7823.</li>
  <li><strong>Close port 7823.</strong> Once the panel has a domain, only ports 80, 443 and 22 need to be open — see <a href="/guides?g=system-settings">System settings</a>.</li>
  <li><strong>Store the admin password</strong> in a password manager, and give teammates their own login instead of sharing it (<a href="/guides?g=users-roles">Users &amp; roles</a>).</li>
  <li><strong>Set a base domain</strong> if you want every app to get an address — with HTTPS — automatically.</li>
</ol>
<div class="callout callout--warn"><p>As long as the panel runs on plain HTTP, your password travels unencrypted. Restrict port 7823 to your own IP in the firewall until HTTPS is set up.</p></div>

<h2>Updating Cloudbase</h2>
<pre>cloudbase update</pre>
<p>This pulls the latest Cloudbase code, reinstalls dependencies and restarts the service. Running apps are not rebuilt; their containers keep running while the panel restarts. During the restart, visitors of the panel see the “Cloudbase is restarting” page.</p>
<p>Update remote nodes as well — the agent code lives on each node. Run <code>cloudbase update</code> there too. Instances on a node are recovered automatically after its agent restarts (<a href="/guides?g=node-outages">Node outages &amp; recovery</a>).</p>

<h2>The service</h2>
<pre>cloudbase status     # is the service running?
cloudbase logs       # follow the service log
cloudbase restart
cloudbase stop</pre>
<p>Stopping Cloudbase pauses management, not your apps: containers keep running and nginx keeps serving them. See the <a href="/guides?g=cli">CLI reference</a> for every command.</p>

<h2>Lost the admin password</h2>
<p>Run <code>cloudbase password</code> on the server to set a new one. Signed-in users can change their own password under <a href="/settings?s=account">Account</a>.</p>

<h2>Where things live</h2>
<table>
  <tr><th>Path</th><th>Contains</th></tr>
  <tr><td><code>~/.cloudbase/config.yaml</code></td><td>Ports, limits and session length</td></tr>
  <tr><td><code>~/.cloudbase/apps/</code></td><td>The source of every app, as cloned from Git</td></tr>
  <tr><td><code>~/.cloudbase/logs/</code></td><td>Log files, including the node agent’s</td></tr>
  <tr><td><code>~/.cloudbase/</code></td><td>Also the database and credentials — include it in server backups</td></tr>
</table>

<h2>Removing Cloudbase</h2>
<pre>cloudbase uninstall</pre>
<p>Removes the service and the install directory. Export your apps first if you want to keep their configuration (<a href="/guides?g=backups">Backups &amp; moving apps</a>).</p>
`,
},

{
  slug: 'first-app',
  group: 'Getting started',
  title: 'Deploy your first app',
  summary: 'A walkthrough of the New app wizard, from repository to a running URL.',
  keywords: 'new app wizard create deploy repository start command port',
  lead: 'This walks through the <strong>New app</strong> wizard on the overview page. Most fields can be left empty — Cloudbase detects sensible defaults — but knowing what each one does saves you a redeploy.',
  body: `
<h2>1. Type</h2>
<p>Pick what you’re deploying. This decides which of the next fields apply. See <a href="/guides?g=app-types">App types &amp; builds</a> for the details.</p>
<ul>
  <li><strong>Web Service</strong> — anything that listens on a port and serves HTTP: an API, a Next.js or Django app, a dashboard.</li>
  <li><strong>Static Site</strong> — plain HTML/CSS/JS, served by nginx. Optionally built first (Vite, Astro, a React SPA).</li>
  <li><strong>Background Worker</strong> — no port and no URL: Discord bots, queue consumers, scheduled jobs.</li>
</ul>

<h2>2. Source</h2>
<ul>
  <li><strong>Application name</strong> — lowercase, used for the folder, the Docker image and the automatic subdomain.</li>
  <li><strong>Repository URL</strong> — the HTTPS URL of a GitHub repository.</li>
  <li><strong>GitHub token</strong> — only for private repositories. Paste one, or click <em>Saved</em> to pick one stored under <a href="/settings?s=tokens">GitHub tokens</a>.</li>
</ul>

<h2>3. Process</h2>
<ul>
  <li><strong>Start command</strong> — what runs inside the container, e.g. <code>npm start</code>, <code>python main.py</code>, <code>gunicorn app:app -b 0.0.0.0:8000</code>. For static sites this field becomes the <strong>publish directory</strong> (the folder with <code>index.html</code>).</li>
  <li><strong>Build command</strong> — runs once while the image is built, e.g. <code>npm run build</code>. Not run on every start.</li>
  <li><strong>Internal port</strong> — the port your app listens on <em>inside</em> the container. Your app must listen on <code>0.0.0.0</code>, not <code>localhost</code>, or nginx can’t reach it.</li>
</ul>
<div class="callout"><p>The internal port is not the public port. Each instance gets its own host port automatically; nginx maps your domain to all of them.</p></div>

<h2>4. Environment</h2>
<p>Add the variables your app needs, or click <strong>Import .env</strong> to load a file. They are encrypted at rest and injected into every instance — and into the build. More in <a href="/guides?g=environment">Environment variables</a>.</p>

<h2>5. Docker runtime</h2>
<p>Optional limits per instance: CPU (in cores, e.g. <code>0.5</code>), memory (MB), a read-only root filesystem and a size-limited <code>/tmp</code>. Leave empty to give the container whatever the server has.</p>

<h2>After you click Deploy</h2>
<ol class="guide-steps">
  <li>The repository is cloned and the image is built. You can follow every build step in the app’s <strong>Logs</strong> tab under <em>All instances</em>.</li>
  <li>The first instance starts on the primary. While it boots, visitors see the <em>starting</em> page instead of an error.</li>
  <li>Once the app answers on its port, its status turns <strong>running</strong> and the URL in the app header works.</li>
</ol>
<p>If a base domain is configured, the app is immediately reachable at <code>&lt;name&gt;.&lt;base-domain&gt;</code>. Otherwise click <strong>Connect a domain</strong> under the app’s <strong>Settings → Network</strong>, see <a href="/guides?g=domains">Domains &amp; HTTPS</a>.</p>

<h2>When it doesn’t start</h2>
<ul>
  <li><strong>Build failed</strong> — the Logs tab shows the failing step. Usually a missing dependency or a build command that needs an environment variable.</li>
  <li><strong>Container exits right away</strong> — check the instance’s own logs (pick it under <em>Source</em> in the Logs tab). Often a wrong start command.</li>
  <li><strong>502 / starting page stays</strong> — the app listens on a different port than the internal port, or on <code>127.0.0.1</code>.</li>
</ul>
<p>More in <a href="/guides?g=troubleshooting">Troubleshooting</a>.</p>
`,
},

/* ─── Apps ─────────────────────────────────────────────────────────────── */

{
  slug: 'app-types',
  group: 'Apps',
  title: 'App types & builds',
  summary: 'Web services, static sites and workers, generated vs. your own Dockerfile, and build commands.',
  keywords: 'dockerfile build command static worker runtime node python go ruby php',
  lead: 'Every app becomes a Docker image. How that image is made depends on the app type, on whether your repository has its own Dockerfile, and on the build command.',
  body: `
<h2>Web services</h2>
<p>The default. The container runs your start command and must listen on the internal port. nginx routes the app’s domains to every running instance and health-checks new instances during deploys.</p>

<h2>Static sites</h2>
<p>Static sites are served by nginx inside the container — there is no start command to write. You set a <strong>publish directory</strong>: the folder that contains <code>index.html</code> after the build. Leave it empty and Cloudbase looks for the usual places: the repository root, <code>dist</code>, <code>build</code>, <code>out</code>, <code>public</code> and similar.</p>
<p>Add a build command (for example <code>npm ci &amp;&amp; npm run build</code>) for sites that need one. The build runs inside the image build, the output is copied into a small nginx image, and nothing of Node.js ends up in the final container.</p>
<div class="callout"><p>Static sites can’t use a read-only root filesystem: nginx needs to write its temp files. The wizard turns that option off for you.</p></div>

<h2>Background workers</h2>
<p>Workers have no port and no domain. Cloudbase starts the container, restarts it according to its restart policy and streams its logs. Maintenance pages, domains and health checks don’t apply.</p>

<h2>Generated Dockerfile</h2>
<p>Without a Dockerfile in the repository, Cloudbase writes one based on the runtime it detects from the start command:</p>
<table>
  <tr><th>Start command begins with</th><th>Base image</th></tr>
  <tr><td><code>npm</code>, <code>node</code>, <code>yarn</code>, …</td><td><code>node:20-alpine</code> — installs dependencies from <code>package.json</code></td></tr>
  <tr><td><code>python</code>, <code>uvicorn</code>, <code>gunicorn</code>, <code>flask</code></td><td><code>python:3.11-slim</code> — installs <code>requirements.txt</code></td></tr>
  <tr><td><code>ruby</code>, <code>rails</code></td><td><code>ruby:3.2-slim</code> — installs the Gemfile</td></tr>
  <tr><td><code>go</code></td><td>Builds with <code>golang:1.22-alpine</code>, runs the binary on <code>alpine</code></td></tr>
  <tr><td><code>php</code>, <code>composer</code></td><td><code>php:8.2-cli</code></td></tr>
  <tr><td>anything else</td><td><code>ubuntu:22.04</code> with curl and wget</td></tr>
</table>
<p>A generated Dockerfile starts with a <code># cloudbase:generated</code> comment and is rewritten on every build, so don’t edit it on the server — edits are lost.</p>

<h2>Your own Dockerfile</h2>
<p>Commit a <code>Dockerfile</code> to the repository root and Cloudbase uses it unchanged. That’s the way to go when you need system packages, a different base image or a multi-stage build. Things to keep in mind:</p>
<ul>
  <li>The <strong>build command</strong> field is ignored — put build steps in your Dockerfile.</li>
  <li>The <strong>start command</strong> is ignored too; your <code>CMD</code>/<code>ENTRYPOINT</code> wins.</li>
  <li>Your app must still listen on the internal port configured in Cloudbase.</li>
  <li>Environment variables are passed as build arguments only for the <code>ARG</code>s your Dockerfile declares.</li>
</ul>

<h2>Build command vs. start command</h2>
<p>The build command runs <strong>once</strong>, while the image is built. The start command runs <strong>every time</strong> an instance starts. Put compilation, bundling and asset generation in the build command, and keep the start command to just starting the server:</p>
<table>
  <tr><th>Framework</th><th>Build command</th><th>Start command</th></tr>
  <tr><td>Next.js</td><td><code>npm run build</code></td><td><code>npm start</code></td></tr>
  <tr><td>Nuxt</td><td><code>npm run build</code></td><td><code>node .output/server/index.mjs</code></td></tr>
  <tr><td>NestJS / TypeScript</td><td><code>npm run build</code></td><td><code>node dist/main.js</code></td></tr>
  <tr><td>Django</td><td><code>python manage.py collectstatic --noinput</code></td><td><code>gunicorn project.wsgi -b 0.0.0.0:8000</code></td></tr>
</table>
<p>Building in the start command works, but every restart and every new instance then rebuilds from scratch — slower starts and more memory.</p>

<h2>Build caching</h2>
<p>Docker caches each step. As long as your dependency files (<code>package.json</code>, <code>requirements.txt</code>, …) don’t change, dependency installation is reused and a rebuild only redoes the later steps. A change to the dependency file invalidates everything after it.</p>
`,
},

{
  slug: 'environment',
  group: 'Apps',
  title: 'Environment variables',
  summary: 'Adding and importing variables, when they apply and how they’re stored.',
  keywords: 'env variables secrets dotenv import build args',
  lead: 'Environment variables are the place for configuration and secrets: database URLs, API keys, feature flags. Cloudbase stores them encrypted and passes them to every instance of the app.',
  body: `
<h2>Adding variables</h2>
<p>In the app, go to <strong>Settings → Environment Variables</strong>. Add rows one by one, or click <strong>Import .env</strong> to read a file in the usual <code>KEY=value</code> format. Comments, blank lines, <code>export</code> prefixes and quoted values are understood. Imported keys that already exist are updated; others are added.</p>
<pre># .env
DATABASE_URL="postgres://app:secret@db:5432/app"
export NODE_ENV=production
SESSION_SECRET=change-me</pre>

<h2>When changes take effect</h2>
<p>Variables are read when a container starts. After saving, restart the app (or deploy) to apply them. Running instances keep the old values until then.</p>

<h2>Variables during the build</h2>
<p>Variables are also available while the image is built, so a build command like <code>npm run build</code> can read <code>NEXT_PUBLIC_*</code> or similar values. With your own Dockerfile, a variable is passed in only if the Dockerfile declares it with <code>ARG NAME</code>.</p>
<div class="callout callout--warn"><p>Values used during the build can end up inside the image (for example inlined in a JavaScript bundle). Don’t use real secrets in client-side build variables.</p></div>

<h2>Storage</h2>
<p>Variables are encrypted in the Cloudbase database and sent to a node only when it starts an instance. App <strong>exports</strong> leave them out on purpose: after importing an app elsewhere, add its variables again (the <strong>Import .env</strong> button makes that quick).</p>

<h2>Variables Cloudbase sets</h2>
<p>Your app doesn’t need to read a port variable — it should listen on the internal port you configured. Bind to <code>0.0.0.0</code>; a server that only listens on <code>127.0.0.1</code> is unreachable from outside its container.</p>
`,
},

{
  slug: 'deploying',
  group: 'Apps',
  title: 'Deploying updates',
  summary: 'Auto-deploy on push, rolling vs. blue/green, deploy history, rollbacks and failed deploys.',
  keywords: 'deploy rolling blue green zero downtime pull commit rebuild health check rollback auto-deploy cd continuous history branch',
  lead: 'The <strong>Deploy</strong> menu in the app header brings new code live. You choose where the code comes from and how running instances are replaced.',
  body: `
<h2>The Deploy menu</h2>
<table>
  <tr><th>Option</th><th>What happens</th></tr>
  <tr><td>Pull &amp; rolling deploy</td><td>Pull the chosen commit, build, then replace instances one by one.</td></tr>
  <tr><td>Pull &amp; blue/green deploy</td><td>Pull, build, start a full new set of instances next to the old ones, then switch over at once.</td></tr>
  <tr><td>Pull &amp; build only</td><td>Pull and build the new image. Running instances keep the old code until you restart them.</td></tr>
  <tr><td>Rebuild image</td><td>Build the current code again without pulling — useful after changing the build command or a build variable.</td></tr>
  <tr><td>Rolling / blue-green redeploy</td><td>The same strategies, without pulling: rebuild the current code and replace instances.</td></tr>
</table>
<p>The pull options first ask which <strong>commit</strong> to deploy. The newest commit on the branch is preselected; pick an older one to roll back to a known-good version.</p>

<h2>Rolling deploys</h2>
<p>For each running instance, in turn:</p>
<ol class="guide-steps">
  <li>Start a new instance with the new image next to the old one.</li>
  <li>Wait until it passes the health check.</li>
  <li>Add it to nginx, then stop and remove the old instance.</li>
</ol>
<p>You never run more than one extra instance, so rolling deploys work on servers without much spare memory. During the roll, old and new code serve traffic side by side for a short while. An app with a single instance is deployed with blue/green instead, since there’s nothing to roll.</p>

<h2>Blue/green deploys</h2>
<ol class="guide-steps">
  <li>Start a complete new set of instances, on the same nodes as the current ones.</li>
  <li>Health-check all of them.</li>
  <li>Switch nginx to the new set in one step, then stop the old set.</li>
</ol>
<p>Visitors switch from old to new code at one moment, and nothing changes until every new instance is healthy. The price is that you briefly need double the capacity.</p>

<h2>Health checks</h2>
<p>A new instance counts as healthy when an HTTP <code>GET /</code> on its port returns any status below 500 — a redirect or 404 is fine, a crash or 502 isn’t. Cloudbase waits up to 10 minutes and logs progress while waiting, so slow-starting apps (large Next.js builds, JVM apps) aren’t cut off.</p>
<p>Instances on remote nodes are checked through their tunnel, after the tunnel has connected.</p>

<h2>When a deploy fails</h2>
<p>If a new instance doesn’t become healthy, the deploy stops and is rolled back: new instances are removed and the old ones keep serving. Nothing is switched in nginx until the new code proves it works. Read the reason in the deploy log, fix it, and deploy again.</p>

<h2>Auto-deploy</h2>
<p>Turn on <strong>Auto-deploy</strong> in the app’s settings and every new commit on the chosen branch is put live by itself, with the strategy you pick. Cloudbase checks the branch every minute (or every 5 or 15 minutes) by asking the Git host for its latest commit — that’s tiny and works from anywhere, also when the panel isn’t reachable from the internet. No webhook or extra GitHub permission is needed.</p>
<ul>
  <li>Several pushes in a row deploy only the newest commit.</li>
  <li>A push during a running deploy waits; the next check picks it up.</li>
  <li>Only commits that were never deployed before go out automatically. A commit whose deploy failed isn’t retried over and over — push a fix, or click <strong>Retry</strong>.</li>
  <li>An app that isn’t running is only built; it starts with the new code next time.</li>
  <li>Apps without a domain, like background workers, can’t be swapped through nginx: their image is rebuilt and the running instances restarted.</li>
</ul>

<h2>Deploy history and rollbacks</h2>
<p>The <strong>Deployments</strong> tab lists every deploy — automatic, from the Deploy menu, or a rollback — with the commit, who or what started it, how long it took and its full log. The deploy that is currently live is marked <em>Live</em>.</p>
<p><strong>Roll back to this</strong> on an older deploy builds that commit again and rolls it out the same way. The rollback stays live until the next push — auto-deploy doesn’t put the newer commit back by itself.</p>

<h2>Following a deploy</h2>
<p>Deploys from the menu open a live log of every step: git, the image build and each instance replacement. The same log is kept in the <strong>Deployments</strong> tab, so you can close the dialog and come back later. Only one deploy runs per app at a time.</p>

<div class="callout callout--tip"><p>Want a maintenance page during a deploy that changes the database? Turn on <strong>update mode</strong> first (<a href="/guides?g=maintenance-pages">Maintenance pages</a>), deploy, then turn it off.</p></div>
`,
},

{
  slug: 'instances',
  group: 'Apps',
  title: 'Instances & scaling',
  summary: 'Adding instances on any node, restart policies, crash protection and autoscaling.',
  keywords: 'instances replicas scaling autoscaling restart policy crash ports',
  lead: 'Every instance is one container. More instances means more capacity and no single point of failure — nginx spreads traffic over all healthy ones.',
  body: `
<h2>Adding and removing instances</h2>
<p>On the app’s <strong>Instances</strong> tab, click <strong>Add instance</strong> and pick the node it should run on. The new instance gets a free host port from the instance range and is added to nginx as soon as it runs. <strong>Remove</strong> stops the container and takes it out of the load balancer.</p>
<p>The instance list shows per instance: status, node, host port, tunnel port (remote nodes), CPU and memory, uptime and the container ID. The container ID matches what <code>docker ps</code> shows on that server.</p>

<h2>Statuses</h2>
<table>
  <tr><th>Status</th><th>Meaning</th></tr>
  <tr><td><code>starting</code></td><td>Building, creating the container, or waiting for its tunnel.</td></tr>
  <tr><td><code>running</code></td><td>Container is up and receiving traffic.</td></tr>
  <tr><td><code>stopped</code></td><td>Stopped on purpose, or exited with restart policy “no”.</td></tr>
  <tr><td><code>error</code></td><td>Failed to start, or crashed too often. The reason is shown with the instance.</td></tr>
  <tr><td><code>node offline</code></td><td>The node or its tunnel is unreachable. Recovered automatically when it’s back.</td></tr>
</table>

<h2>Restart policy</h2>
<p>Under <strong>Settings → Runtime</strong>:</p>
<ul>
  <li><strong>No</strong> — a crashed instance stays stopped.</li>
  <li><strong>On failure</strong> — restart when the process exits with an error.</li>
  <li><strong>Always</strong> — restart on any exit.</li>
</ul>
<p><strong>Auto-start on boot</strong> starts the app’s instances when Cloudbase itself starts, for example after a server reboot.</p>

<h2>Crash protection</h2>
<p>An instance that keeps crashing is not restarted forever. After 5 restarts within 60 seconds (configurable under <a href="/settings?s=system">System settings</a>), Cloudbase gives up and marks it as <code>error</code>. Fix the cause, then start it again.</p>

<h2>Autoscaling</h2>
<p>Turn on <strong>autoscaling</strong> in the app settings and set a minimum, a maximum and a CPU target. Every minute Cloudbase looks at the app’s average CPU over the last two minutes:</p>
<ul>
  <li>Above the target and below the maximum → one instance is added on the primary.</li>
  <li>Below half the target and above the minimum → one instance is removed.</li>
</ul>
<p>After every scaling step the app is left alone for two minutes, so short spikes don’t cause flapping. Autoscaling only acts on apps that are running.</p>

<h2>Resource limits</h2>
<p>CPU and memory limits apply per instance. A container that exceeds its memory limit is killed by Docker and restarted according to the restart policy — if you see instances restarting under load, raise the limit or add instances instead.</p>
`,
},

{
  slug: 'logs-metrics',
  group: 'Apps',
  title: 'Logs & metrics',
  summary: 'Live logs per instance, build output, resource charts and history.',
  keywords: 'logs metrics cpu memory network disk monitoring history activity audit',
  lead: 'Every app has live logs and resource charts, kept even while you’re not looking.',
  body: `
<h2>Logs</h2>
<p>The <strong>Logs</strong> tab streams output live. The <em>Source</em> picker chooses what you see:</p>
<ul>
  <li><strong>All instances</strong> — output of every instance merged, plus Cloudbase’s own messages for the app: builds, deploy steps, starts and stops.</li>
  <li><strong>A single instance</strong> — only that container’s output, also for instances on remote nodes.</li>
</ul>
<p>When the selected instance isn’t running — stopped, failed, or on a node that’s offline — the log view tells you so instead of waiting for output, and switches back to live logs as soon as the instance runs again. The last 300 lines are shown when you open a stream.</p>

<h2>Metrics</h2>
<p>The <strong>Metrics</strong> tab shows CPU, memory, network and disk I/O for the app (all instances together) and per instance. Numbers come from Docker’s container statistics, also for remote nodes.</p>

<h2>History</h2>
<p>Cloudbase samples the overview and every node in the background every 30 seconds and keeps seven days of history. Charts on the overview and node pages are filled from that history, so they show data as soon as you open them. On a node page you can switch between the last hour, 24 hours and 7 days.</p>

<h2>Activity and audit log</h2>
<p>The app’s <strong>Activity</strong> tab lists everything that happened to that app: deploys, restarts, setting changes, who did it and when. The <a href="/audit">Audit log</a> shows the same for the whole installation — apps, nodes, users and settings.</p>

<h2>Logs on the server</h2>
<pre>cloudbase logs                         # the Cloudbase service
docker logs -f cloudbase-app-3-replica-12   # one instance, by container name
tail -f ~/.cloudbase/logs/node-agent.log    # the agent on a node</pre>
<p>Instance containers are named <code>cloudbase-app-&lt;app id&gt;-replica-&lt;instance id&gt;</code>.</p>
`,
},

/* ─── Networking ───────────────────────────────────────────────────────── */

{
  slug: 'domains',
  group: 'Networking',
  title: 'Domains & HTTPS',
  summary: 'Connect domains, free automatic HTTPS, the panel domain, app subdomains and Cloudflare.',
  keywords: 'domain dns ssl tls https certificate wildcard nginx subdomain redirect letsencrypt connect wizard certbot cloudflare renew expiry',
  lead: 'Every domain points at the primary, where nginx routes it to the right app and serves HTTPS. Certificates come free from Let’s Encrypt, are requested in one click and renew by themselves.',
  body: `
<h2>Connect a domain to an app</h2>
<p>In the app, open <strong>Settings → Network</strong> and click <strong>Connect a domain</strong>:</p>
<ol class="guide-steps">
  <li><strong>Domain</strong> — type the name, for example <code>shop.example.com</code>. If the app already has a domain, choose whether the new one <em>shows the app</em> too or <em>redirects</em> to the main domain. For a bare domain like <code>example.com</code> you can tick <code>www.example.com</code> as well.</li>
  <li><strong>DNS</strong> — the wizard shows the record to add at your domain provider, with copy buttons, and checks every few seconds until the domain reaches this server.</li>
  <li><strong>HTTPS</strong> — one click requests the certificate and switches the app to HTTPS.</li>
</ol>
<div class="callout"><p>Automatic HTTPS needs <code>certbot</code> on the primary and ports 80 and 443 reachable from the internet. Older installations get it with <code>cloudbase update</code> followed by <code>cloudbase nginx permissions</code>.</p></div>

<h2>The DNS record</h2>
<p>Every name points at the <strong>primary</strong>’s public IP — also for apps whose instances run on other nodes, because traffic always enters through the primary.</p>
<table>
  <tr><th>Record</th><th>For</th></tr>
  <tr><td><code>panel.example.com  A  203.0.113.10</code></td><td>The panel</td></tr>
  <tr><td><code>*.apps.example.com  A  203.0.113.10</code></td><td>Automatic app subdomains</td></tr>
  <tr><td><code>shop.example.com  A  203.0.113.10</code></td><td>One app’s own domain</td></tr>
</table>
<p>The wizard asks your domain’s own nameservers directly, so a new record shows up the moment it’s saved — your computer or router may still show the old answer for a while. A brand-new record usually works within minutes; a changed record can take as long as its old TTL (often an hour).</p>
<div class="callout callout--warn"><p>Remove any <strong>AAAA (IPv6) record</strong> that doesn’t point to this server. Let’s Encrypt tries IPv6 first, so a stray AAAA record makes the certificate request fail even when the A record is right. The wizard warns you when it sees one.</p></div>

<h2>Managing an app’s domains</h2>
<p><strong>Settings → Network</strong> lists every domain the app answers on, with a lock for HTTPS and what each domain does. The <strong>⋯</strong> menu on a domain offers:</p>
<ul>
  <li><strong>Make primary</strong> — the main address of the app. The old primary keeps showing the app.</li>
  <li><strong>Redirect to …</strong> / <strong>Show the app here</strong> — send visitors to the primary domain, or serve the app on this one too.</li>
  <li><strong>Remove</strong> — the certificate is renewed for the remaining names; after the last domain it’s deleted. The app then falls back to its automatic subdomain, if there’s a base domain.</li>
</ul>
<p>Below the list you see until when the certificate is valid. A yellow or red line means renewal is failing — see <a href="/guides?g=troubleshooting">Troubleshooting</a>.</p>

<h2>How certificates work</h2>
<p>Each app has <strong>one certificate</strong> that covers all of its domains — <code>shop.example.com</code>, <code>example.com</code> and <code>www.example.com</code> on one app share a certificate, even across different domains. Another app gets its own. Certificates are valid for 90 days.</p>
<ol class="guide-steps">
  <li>Cloudbase asks Let’s Encrypt for a certificate through <code>certbot</code>.</li>
  <li>Let’s Encrypt checks each name by fetching a file from <code>http://&lt;domain&gt;/.well-known/acme-challenge/</code>. nginx serves that folder on every domain, also on names not linked to an app yet.</li>
  <li>The certificate is stored in <code>/etc/letsencrypt/live/&lt;name&gt;/</code> and nginx switches to HTTPS.</li>
</ol>
<p><strong>Renewal</strong> runs without Cloudbase: certbot’s timer checks twice a day and renews certificates that have less than 30 days left, then reloads nginx. It works while Cloudbase is stopped, as long as the domains still point here and port 80 is open.</p>
<pre>sudo certbot certificates          # all certificates, names and expiry
sudo certbot renew --dry-run       # test renewal without changing anything
systemctl list-timers | grep certbot</pre>
<p>Certificates are public: anyone can see which names share one, and Let’s Encrypt publishes all certificates in open logs.</p>

<h2>Panel domain</h2>
<p>Under <a href="/settings?s=domain">Settings → Domain &amp; SSL</a>, click <strong>Connect a domain</strong> next to the panel. It’s the same wizard as for apps: the domain, the DNS record with a live check, and a free certificate. The card then shows until when it’s valid.</p>
<p>Set up the panel earlier with a certificate of your own, like a Cloudflare Origin certificate? It keeps working, and the card says so. <strong>Switch to a free certificate</strong> replaces it with one that renews itself.</p>
<p>The panel’s nginx config is rewritten every time Cloudbase starts, so it always serves the verification folder Let’s Encrypt needs.</p>
<p>Saving also regenerates the page shown while Cloudbase restarts and the page for hostnames that aren’t linked to an app.</p>

<h2>Automatic app subdomains</h2>
<p>Click <strong>Set up app subdomains</strong> under <a href="/settings?s=domain">Settings → Domain &amp; SSL</a> and enter a base domain such as <code>apps.example.com</code>. The wizard shows the one record to add — a wildcard <code>*.apps</code> A record pointing at the primary — and checks it with a test name under it. Every app is then reachable at <code>&lt;app-name&gt;.apps.example.com</code>, and new apps work without new records.</p>
<p>HTTPS is automatic: each app gets its own free certificate for its subdomain, usually within a minute after it starts. Let’s Encrypt can only issue a wildcard certificate through DNS verification, which would need access to your DNS provider — one certificate per app avoids that. A wildcard certificate set up earlier keeps working until you click <strong>Switch to automatic</strong>.</p>
<p>A failed automatic request is retried after an hour. The app’s <strong>Settings → Network</strong> shows the state of its subdomain.</p>

<h2>Cloudflare</h2>
<p>The proxy (orange cloud) can stay on, as long as <strong>Always Use HTTPS</strong> is off: Let’s Encrypt checks over plain HTTP, and that setting would redirect it. If the DNS check in the wizard stays red, set the record to <em>DNS only</em> (grey) for a moment. Once HTTPS works, use SSL mode <strong>Full (strict)</strong>.</p>
<p>Behind the proxy, Cloudflare’s 526 error means the server showed a certificate Cloudflare doesn’t accept for that name — usually because the app has no certificate of its own yet. Set up HTTPS for it as above.</p>

<h2>Servers at home</h2>
<p>Behind a home router, visitors reach your public IP address — the router has to pass ports <strong>80</strong> and <strong>443</strong> on to the server (port forwarding). The wizard notices when the server is behind a router and shows the local address to forward to, for example <code>192.168.1.20</code>. Give the server a fixed local address in the router, so the forwarding keeps working after a restart.</p>
<p>Such a server often can’t reach its own public address (no “NAT loopback”). The wizard then checks nginx locally instead; the real test happens when Let’s Encrypt connects from outside.</p>
<p>If your home connection’s public IP changes now and then, the DNS records need to follow it — use your provider’s dynamic DNS, or Cloudflare with a DDNS updater.</p>

<h2>Unknown hostnames</h2>
<p>A hostname that reaches the server but isn’t linked to any app gets a neutral “Nothing is deployed here” page, instead of accidentally showing another app.</p>
`,
},

{
  slug: 'maintenance-pages',
  group: 'Networking',
  title: 'Maintenance pages',
  summary: 'Downtime and update mode, start and restart pages, and custom HTML.',
  keywords: 'maintenance downtime update mode restart starting page custom html logo status',
  lead: 'nginx can show a page instead of your app: on purpose (downtime or update mode) or automatically while the app starts or restarts. Visitors get a clear message instead of a browser error.',
  body: `
<h2>The four pages</h2>
<table>
  <tr><th>Page</th><th>When it’s shown</th></tr>
  <tr><td>Downtime</td><td>When you turn on <strong>downtime mode</strong>, and automatically when no instance is reachable (nginx gets a 502/503/504).</td></tr>
  <tr><td>Update</td><td>When you turn on <strong>update mode</strong> — for planned maintenance, migrations or longer deploys.</td></tr>
  <tr><td>Starting</td><td>While an app starts for the first time, until it answers.</td></tr>
  <tr><td>Restart</td><td>During a restart, and removed as soon as the app is healthy again.</td></tr>
</table>
<p>Downtime and update mode are toggled from the <strong>⋯</strong> menu in the app header. They stay on until you turn them off.</p>

<h2>Customising a page</h2>
<p>Each page has its own template fields: title, message, accent colour, a logo and an optional link to a status page. For full control, paste your own HTML instead — it’s served as-is, so inline your CSS and images. Use <strong>Preview</strong> to check before saving.</p>

<h2>“Requires nginx”</h2>
<p>Maintenance pages are served by nginx in front of your app, so they only work for apps that nginx serves: apps with a custom domain, or reachable through the base domain. You’ll see <em>Requires nginx</em> when neither is set up. Workers never have these pages — there’s nothing in front of them.</p>
<p>If a domain is set but the label still shows, writing the nginx config failed earlier. Click <strong>Save Changes</strong> in the app’s settings to write it again.</p>

<h2>Status codes</h2>
<p>Maintenance pages are served with HTTP <code>503</code> and no-cache headers, so search engines treat the outage as temporary and browsers don’t keep showing the page after you’re back.</p>
`,
},

/* ─── Nodes ────────────────────────────────────────────────────────────── */

{
  slug: 'nodes',
  group: 'Nodes',
  title: 'Adding nodes',
  summary: 'Connect more servers, how tunnels work, and node-only vs. panel+node mode.',
  keywords: 'node add connect invite agent tunnel remote server mode',
  lead: 'A node is an extra server that runs instances for the primary. Nodes are controlled by a small agent and don’t need any inbound ports.',
  body: `
<h2>Connecting a node</h2>
<ol class="guide-steps">
  <li>Install Cloudbase on the new server — Linux with systemd and a <code>sudo</code> user; the installer adds Docker, nginx, Git and Python if they’re missing:
<pre>git clone https://github.com/Tonioot/Cloudbase
cd Cloudbase
sudo bash install.sh</pre></li>
  <li>On the overview, click <strong>Add node</strong>. Cloudbase creates an invite code that’s valid for 30 minutes and shows the command to run.</li>
  <li>Run that command on the new server:
<pre>cloudbase connect --main-url https://panel.example.com --invite-code &lt;code&gt; --mode node-only</pre></li>
  <li>The node appears in the sidebar under <em>Nodes</em> and turns online within a few seconds.</li>
</ol>
<p>The node registers once with the invite code and gets its own long-lived token. The code can’t be used again. Use <code>--node-name</code> to choose its name; by default it’s the server’s hostname.</p>

<h2>Modes</h2>
<ul>
  <li><strong>node-only</strong> — just the agent and the local runtime. The usual choice for extra servers.</li>
  <li><strong>panel+node</strong> — the node also runs its own panel. Only useful in special setups.</li>
</ul>

<h2>How the connection works</h2>
<p>The agent keeps an outbound WebSocket open to the primary. Over it, the primary sends commands (start, stop, build, fetch logs or stats) and the agent reports heartbeats with CPU, memory and disk usage.</p>
<p>For each instance on the node, the agent opens a separate <strong>tunnel</strong> to the primary. On the primary the tunnel becomes a local port (from the tunnel range, <code>127.0.0.1</code> only) that nginx adds to the app’s load balancer. Remote instances therefore need no open ports, no public IP and no VPN.</p>

<h2>Running instances on a node</h2>
<p>Add an instance to an app and pick the node. The first time, the node downloads the app’s source from the primary and builds the image itself; later deploys only rebuild what changed. Deploys and restarts work exactly as on the primary.</p>

<h2>The node page</h2>
<p>Click a node in the sidebar to see its CPU, memory and disk over time, the instances it runs, the commands the primary sent it, and the agent’s own log. Rename, disable or remove the node from there.</p>

<h2>Removing a node</h2>
<p>Move or remove all instances on the node first — a node with instances can’t be deleted. Then remove it in the panel and run <code>cloudbase disconnect</code> on the server.</p>
`,
},

{
  slug: 'node-outages',
  group: 'Nodes',
  title: 'Node outages & recovery',
  summary: 'What happens when a node or tunnel drops, and how instances come back by themselves.',
  keywords: 'offline node_offline recovery reconnect tunnel orphan cleanup restart',
  lead: 'Nodes go offline: reboots, network blips, updates. Cloudbase is built to keep serving from the remaining instances and to restore the rest without your help.',
  body: `
<h2>When a node goes offline</h2>
<p>The primary marks a node offline when its WebSocket closes or its heartbeats stop. Its running instances become <code>node offline</code>, their tunnels close and nginx stops sending them traffic. Instances on other nodes keep serving.</p>

<h2>When it comes back</h2>
<p>As soon as the agent is connected again, the primary sends a start command for every <code>node offline</code> instance on it:</p>
<ul>
  <li>If the container survived (only the connection dropped), the agent just reconnects the tunnel. The container isn’t restarted.</li>
  <li>If the container is gone (the server rebooted), it’s started again from the image on the node — rebuilt first if needed.</li>
</ul>
<p>The instance goes through <code>starting</code> back to <code>running</code> and rejoins the load balancer. The app’s log shows <em>Node came back online — recovering instance …</em>.</p>

<h2>When only a tunnel drops</h2>
<p>A tunnel can drop while the node stays connected, for example when the agent restarts. The instance is then also marked <code>node offline</code> and recovered the same way, so a running container is never mistaken for a stopped one.</p>

<h2>Orphaned containers</h2>
<p>Every few minutes the agent compares the instance containers on its server with what the primary expects. A container the primary doesn’t know (anymore) — for example of an instance you deleted while the node was offline — is removed. To prevent mistakes during restarts, a container is only removed when it’s older than two minutes and two checks in a row agree. Stop commands queued while a node was offline are still carried out when it reconnects.</p>

<h2>Checking by hand</h2>
<pre>cloudbase node-status                        # on the node: is the agent connected?
docker ps --filter name=cloudbase-app        # which instance containers exist
tail -n 100 ~/.cloudbase/logs/node-agent.log # what the agent did</pre>
<p>If an instance stays <code>node offline</code> while its node is online, open the node page and look at the agent log and the command list — a failed start shows its error there.</p>
`,
},

/* ─── Administration ───────────────────────────────────────────────────── */

{
  slug: 'users-roles',
  group: 'Administration',
  title: 'Users & roles',
  summary: 'Giving others access, built-in roles and every permission explained.',
  keywords: 'users roles permissions access rbac admin viewer team',
  lead: 'Give teammates their own login and only the rights they need. Permissions are grouped into roles; every user has exactly one role.',
  body: `
<h2>Root, Administrator and Viewer</h2>
<ul>
  <li><strong>Root</strong> — the <code>admin</code> account created at install. Always has every permission and can’t be deleted.</li>
  <li><strong>Administrator</strong> — built-in role with every permission.</li>
  <li><strong>Viewer</strong> — built-in read-only role: apps, nodes and the audit log.</li>
</ul>
<p>Create your own roles for anything in between, under <a href="/settings?s=users">Settings → Users &amp; roles</a>.</p>

<h2>All permissions</h2>
<table>
  <tr><th>Permission</th><th>Allows</th></tr>
  <tr><td><code>apps.view</code></td><td>See apps, their status, logs and metrics</td></tr>
  <tr><td><code>apps.deploy</code></td><td>Create new apps</td></tr>
  <tr><td><code>apps.start</code> / <code>apps.stop</code> / <code>apps.restart</code></td><td>Start, stop and restart apps</td></tr>
  <tr><td><code>apps.pull</code></td><td>Pull code, rebuild images and run deploys</td></tr>
  <tr><td><code>apps.scale</code></td><td>Add and remove instances</td></tr>
  <tr><td><code>apps.configure</code></td><td>Edit settings, domains, certificates, environment variables and maintenance pages; export/import apps</td></tr>
  <tr><td><code>apps.delete</code></td><td>Delete apps</td></tr>
  <tr><td><code>nodes.view</code></td><td>See nodes and their status</td></tr>
  <tr><td><code>nodes.add</code></td><td>Create node invites</td></tr>
  <tr><td><code>nodes.configure</code></td><td>Rename, enable and disable nodes</td></tr>
  <tr><td><code>nodes.delete</code></td><td>Remove nodes</td></tr>
  <tr><td><code>audit.view</code></td><td>Read the audit log</td></tr>
  <tr><td><code>system.manage</code></td><td>Domain &amp; SSL, system settings, server logs and nginx</td></tr>
  <tr><td><code>users.manage</code></td><td>Create, edit and delete users</td></tr>
  <tr><td><code>roles.manage</code></td><td>Create, edit and delete roles</td></tr>
  <tr><td><code>tokens.manage</code></td><td>Manage saved GitHub tokens</td></tr>
</table>
<p>The panel hides what a user isn’t allowed to do, and the API refuses it as well — hiding a button is not the only protection.</p>

<h2>Example roles</h2>
<table>
  <tr><th>Role</th><th>Permissions</th></tr>
  <tr><td>Developer</td><td><code>apps.view</code>, <code>apps.start</code>, <code>apps.stop</code>, <code>apps.restart</code>, <code>apps.pull</code>, <code>apps.scale</code>, <code>nodes.view</code></td></tr>
  <tr><td>Operator</td><td>Developer + <code>apps.configure</code>, <code>nodes.add</code>, <code>nodes.configure</code>, <code>audit.view</code></td></tr>
  <tr><td>Support</td><td><code>apps.view</code>, <code>apps.restart</code>, <code>audit.view</code></td></tr>
</table>

<h2>Sessions</h2>
<p>A sign-in is valid for one hour by default; change that under <a href="/settings?s=system">System settings</a>. The remaining time is shown in the account menu at the bottom of the sidebar. Changing your own password signs you out, so you log in again with the new one.</p>
`,
},

{
  slug: 'github-tokens',
  group: 'Administration',
  title: 'Private repositories',
  summary: 'Creating a GitHub token, saving it in Cloudbase and using it for apps.',
  keywords: 'github token private repository pat fine-grained access',
  lead: 'Public repositories need nothing. For private ones, Cloudbase uses a GitHub access token to clone and pull.',
  body: `
<h2>Creating a token on GitHub</h2>
<ol class="guide-steps">
  <li>On GitHub, go to <strong>Settings → Developer settings → Personal access tokens → Fine-grained tokens</strong>.</li>
  <li>Choose the repositories Cloudbase should deploy (or the organisation as resource owner).</li>
  <li>Under repository permissions, set <strong>Contents</strong> to <em>Read-only</em>. Nothing else is needed.</li>
  <li>Pick an expiry date and generate the token.</li>
</ol>
<p>A classic token works too; it needs the <code>repo</code> scope, which grants more than necessary.</p>

<h2>Saving it in Cloudbase</h2>
<p>Under <a href="/settings?s=tokens">Settings → GitHub tokens</a>, give the token a label and save it. It’s stored encrypted and only its last four characters are shown again. When creating or editing an app, click <strong>Saved</strong> next to the token field to pick it.</p>
<p>You can also paste a token directly into an app; it’s then stored with that app only.</p>

<h2>When a token expires</h2>
<p>Pulls and deploys fail with an authentication error, while running instances keep running. Create a new token, save it under the same label or a new one, and select it in the affected apps’ settings.</p>
`,
},

{
  slug: 'backups',
  group: 'Administration',
  title: 'Backups & moving apps',
  summary: 'App exports, full backups with the CLI, and moving to a new server.',
  keywords: 'backup export import restore migrate move server json tar',
  lead: 'There are two kinds of backup: app configuration as JSON from the panel, and a full backup of the Cloudbase database and credentials from the command line.',
  body: `
<h2>Exporting apps (panel)</h2>
<p>Under <a href="/settings?s=transfer">Settings → Export / import</a>, select apps and download a JSON file. It contains each app’s configuration: repository, type, build and start command, port, domains and certificate paths, restart policy, Docker limits and maintenance pages.</p>
<p>Deliberately <strong>not</strong> included: environment variables and GitHub tokens (secrets don’t belong in a file you pass around), source code (it comes from Git), and container data. Keep a copy of each app’s <code>.env</code> somewhere safe so you can re-import it.</p>

<h2>Importing apps</h2>
<p>Choose the file on the same page. Cloudbase creates each app with one instance and deploys it from its repository. Apps whose name already exists are skipped. Use <strong>Target node</strong> to put everything on one node; otherwise the primary is used. Afterwards, add environment variables and tokens, and scale up where needed.</p>

<h2>Full backup (CLI)</h2>
<pre>cloudbase export ~/cloudbase-backup.tar.gz
cloudbase import ~/cloudbase-backup.tar.gz</pre>
<p>This includes the database (apps, nodes, users, roles, history) and the credentials Cloudbase needs to decrypt secrets. Use it for disaster recovery of the same installation.</p>

<h2>Moving to a new server</h2>
<ol class="guide-steps">
  <li>Install Cloudbase on the new server.</li>
  <li>Export your apps on the old panel and import them on the new one. Add their environment variables and tokens again.</li>
  <li>Set the panel and base domain under Domain &amp; SSL. Certificates aren’t part of an export: click <strong>Set up HTTPS</strong> for the panel and for each app once DNS points to the new server.</li>
  <li>Move DNS to the new server’s IP.</li>
  <li>Re-connect remote nodes with a new invite from the new panel.</li>
</ol>

<h2>Your app’s data</h2>
<p>Cloudbase doesn’t back up data your app writes inside its container. Containers are replaced on every deploy — keep state in an external database or object storage, and back that up separately.</p>
`,
},

{
  slug: 'system-settings',
  group: 'Administration',
  title: 'System settings',
  summary: 'Session length, port ranges, limits and where configuration lives.',
  keywords: 'config yaml ports limits session token expire settings',
  lead: 'The <a href="/settings?s=system">System settings</a> page edits <code>~/.cloudbase/config.yaml</code>. The file is kept across updates.',
  body: `
<h2>Settings and defaults</h2>
<table>
  <tr><th>Setting</th><th>Default</th><th>Notes</th></tr>
  <tr><td>Session length</td><td><code>3600</code> s</td><td>How long a sign-in stays valid.</td></tr>
  <tr><td>Instance ports</td><td><code>10000–19999</code></td><td>Host ports for instances. Needs at least as many ports as the instance limit. Restart required.</td></tr>
  <tr><td>Tunnel ports</td><td><code>20000–29999</code></td><td>Local ports on the primary for remote instances. Restart required.</td></tr>
  <tr><td>Max apps</td><td><code>1000</code></td><td></td></tr>
  <tr><td>Max instances</td><td><code>10000</code></td><td>Across all apps and nodes.</td></tr>
  <tr><td>Max nodes</td><td><code>100</code></td><td>Including the primary.</td></tr>
  <tr><td>Crash protection</td><td><code>5</code> restarts in <code>60</code> s</td><td>Then the instance is marked as failed.</td></tr>
</table>

<h2>Firewall</h2>
<p>Only these ports need to be open to the internet on the primary: <code>80</code> and <code>443</code> for nginx, and <code>22</code> for SSH. At home, forward <code>80</code> and <code>443</code> on your router to the primary. Port <code>7823</code> can be closed once the panel has a domain. Instance and tunnel ports don’t need to be public — nginx reaches them locally. Remote nodes need no inbound ports at all.</p>

<h2>The config file</h2>
<pre>~/.cloudbase/config.yaml</pre>
<p>You can edit it by hand as well; restart Cloudbase afterwards. The panel port itself (<code>server.port</code>) can only be changed there.</p>
`,
},

/* ─── Reference ────────────────────────────────────────────────────────── */

{
  slug: 'cli',
  group: 'Reference',
  title: 'CLI reference',
  summary: 'Every cloudbase command for the service, nodes, nginx, certificates and backups.',
  keywords: 'cli command line terminal cloudbase commands',
  lead: 'The <code>cloudbase</code> command manages the service and the server. Day-to-day app work happens in the panel.',
  body: `
<h2>Service</h2>
<table>
  <tr><th>Command</th><th>Does</th></tr>
  <tr><td><code>cloudbase start</code></td><td>Start Cloudbase</td></tr>
  <tr><td><code>cloudbase stop</code></td><td>Stop Cloudbase (app containers keep running)</td></tr>
  <tr><td><code>cloudbase restart</code></td><td>Restart Cloudbase</td></tr>
  <tr><td><code>cloudbase status</code></td><td>Show the service status</td></tr>
  <tr><td><code>cloudbase logs</code></td><td>Follow the service log</td></tr>
  <tr><td><code>cloudbase enable</code></td><td>Install the systemd service and start on boot</td></tr>
  <tr><td><code>cloudbase disable</code></td><td>Remove autostart and stop the service</td></tr>
  <tr><td><code>cloudbase update</code></td><td>Pull the latest Cloudbase, reinstall dependencies, restart</td></tr>
  <tr><td><code>cloudbase uninstall</code></td><td>Remove Cloudbase from the server</td></tr>
</table>

<h2>Information</h2>
<table>
  <tr><th>Command</th><th>Does</th></tr>
  <tr><td><code>cloudbase apps</code></td><td>List all apps</td></tr>
  <tr><td><code>cloudbase nodes</code></td><td>List all nodes</td></tr>
  <tr><td><code>cloudbase password</code></td><td>Set a new admin password</td></tr>
</table>

<h2>Nodes</h2>
<table>
  <tr><th>Command</th><th>Does</th></tr>
  <tr><td><code>cloudbase connect --main-url &lt;url&gt; --invite-code &lt;code&gt;</code></td><td>Connect this server as a node. Options: <code>--node-name</code>, <code>--mode node-only|panel+node</code></td></tr>
  <tr><td><code>cloudbase disconnect</code></td><td>Forget the connection to the primary</td></tr>
  <tr><td><code>cloudbase node-status</code></td><td>Show the node’s registration and whether the agent runs</td></tr>
</table>

<h2>nginx</h2>
<table>
  <tr><th>Command</th><th>Does</th></tr>
  <tr><td><code>cloudbase nginx &lt;domain&gt;</code></td><td>Serve the panel on a domain</td></tr>
  <tr><td><code>cloudbase nginx show</code></td><td>Print the panel’s nginx config</td></tr>
  <tr><td><code>cloudbase nginx disable</code></td><td>Remove the panel’s nginx config</td></tr>
  <tr><td><code>cloudbase nginx permissions</code></td><td>Let Cloudbase manage nginx without sudo prompts</td></tr>
</table>

<h2>Certificates</h2>
<table>
  <tr><th>Command</th><th>Does</th></tr>
  <tr><td><code>cloudbase cert add &lt;path&gt; [name]</code></td><td>Copy a certificate or key into the store</td></tr>
  <tr><td><code>cloudbase cert list</code></td><td>List stored certificates</td></tr>
  <tr><td><code>cloudbase cert path</code></td><td>Print the store directory</td></tr>
</table>

<h2>Backups</h2>
<table>
  <tr><th>Command</th><th>Does</th></tr>
  <tr><td><code>cloudbase export [file]</code></td><td>Write database and credentials to a <code>.tar.gz</code></td></tr>
  <tr><td><code>cloudbase import &lt;file&gt;</code></td><td>Restore from such a file</td></tr>
</table>
`,
},

{
  slug: 'troubleshooting',
  group: 'Reference',
  title: 'Troubleshooting',
  summary: 'The common problems — failed builds, 502s, stuck instances — and how to find the cause.',
  keywords: 'troubleshooting error 502 failed build stuck offline debug problem fix',
  lead: 'Most problems show up in one of three places: the app’s Logs tab, the instance list, or the node page. Start there.',
  body: `
<h2>The build fails</h2>
<ul>
  <li>Open <strong>Logs → All instances</strong>: the failing build step and its output are there.</li>
  <li><em>Missing module / package</em> — the dependency isn’t in <code>package.json</code> or <code>requirements.txt</code>.</li>
  <li><em>Build needs a variable</em> — add it under Environment Variables; they’re available during the build. With your own Dockerfile, declare it with <code>ARG</code>.</li>
  <li><em>Out of memory during build</em> — large frontend builds can need 2 GB+. Add swap or build on a bigger node.</li>
</ul>

<h2>502 or the starting page stays</h2>
<ul>
  <li>The app listens on another port than the <strong>internal port</strong> setting.</li>
  <li>The app listens on <code>127.0.0.1</code>/<code>localhost</code> instead of <code>0.0.0.0</code>.</li>
  <li>The container exits right after starting — pick the instance under <em>Source</em> in the Logs tab to see why.</li>
  <li>The app answers <code>GET /</code> with a 5xx; health checks treat that as unhealthy.</li>
</ul>

<h2>An instance keeps restarting</h2>
<p>Check its logs for the crash. If it was killed without an error, it probably hit its <strong>memory limit</strong>. After 5 crashes within a minute, Cloudbase stops trying and marks it <code>error</code>.</p>

<h2>An instance stays “node offline”</h2>
<ul>
  <li>Is the node online in the sidebar? If not, check the server and run <code>cloudbase node-status</code> there.</li>
  <li>If the node is online, open its page: the <em>commands</em> list shows whether the recovery start failed, and why.</li>
  <li>Restart the instance by hand from the Instances tab — the agent reuses a running container if there is one.</li>
</ul>

<h2>Status doesn’t match the server</h2>
<p>Compare with the server itself:</p>
<pre>docker ps -a --filter name=cloudbase-app</pre>
<p>Container names are <code>cloudbase-app-&lt;app id&gt;-replica-&lt;instance id&gt;</code>, and the container ID in the instance list matches the first column. A restart of the instance from the panel brings the two back in line.</p>

<h2>HTTPS or certificate problems</h2>
<ul>
  <li><strong>The wizard’s DNS check stays red</strong> — the record isn’t there yet, points elsewhere, or Cloudflare’s proxy is on with <em>Always Use HTTPS</em>. The message says which.</li>
  <li><strong>“A site answered instead of the verification folder”</strong> — an older nginx config handles the domain. Click <strong>Save Changes</strong> in the app’s settings, and remove leftover files for that domain from <code>/etc/nginx/sites-enabled</code>.</li>
  <li><strong>Request fails with a rate limit</strong> — Let’s Encrypt allows only a few failed attempts per hour per name. Fix the cause, wait an hour, try again.</li>
  <li><strong>Browser says the certificate is invalid</strong> — the server showed a certificate for another name, often the panel’s. That app has no certificate yet: use <strong>Set up HTTPS</strong> in its Network settings.</li>
  <li><strong>Expiry warning under the domain list</strong> — renewal is failing. Run <code>sudo certbot renew --dry-run</code> to see why; usually a domain no longer points here, or port 80 is closed.</li>
</ul>

<h2>A domain doesn’t work</h2>
<ul>
  <li><code>dig +short shop.example.com</code> must return the primary’s IP.</li>
  <li>Ports 80 and 443 must be open on the primary.</li>
  <li>Click <strong>Save Changes</strong> in the app’s settings — nginx is written again, and an error is shown if that fails.</li>
  <li>Check nginx itself: <code>sudo nginx -t</code>.</li>
</ul>

<h2>The panel is unreachable</h2>
<ul>
  <li>You see “Cloudbase is restarting”: the service is down. <code>cloudbase status</code> and <code>cloudbase logs</code> on the primary show why.</li>
  <li>Your apps keep running while the panel is down — only managing them is paused.</li>
</ul>

<h2>Where to look</h2>
<table>
  <tr><th>What</th><th>Where</th></tr>
  <tr><td>Cloudbase service</td><td><code>cloudbase logs</code></td></tr>
  <tr><td>Agent on a node</td><td><code>~/.cloudbase/logs/node-agent.log</code>, or the node page</td></tr>
  <tr><td>One instance</td><td>Logs tab → Source, or <code>docker logs &lt;container&gt;</code></td></tr>
  <tr><td>Who changed what</td><td><a href="/audit">Audit log</a></td></tr>
  <tr><td>nginx</td><td><code>sudo nginx -t</code>, <code>/var/log/nginx/error.log</code></td></tr>
</table>
`,
},
];
