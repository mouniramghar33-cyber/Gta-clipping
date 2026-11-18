# AutoClipper installeren (Nederlands)

AutoClipper moet op een computer draaien die **24/7 aan staat**. Je telefoon, iPad en gewone
pc mogen uit; deze machine niet. Het systeem werkt op gewone pc's/servers (x86) én op ARM
(zoals de gratis Oracle-server).

## 1. Kies waar het draait

| Optie | Kosten | Wat kan het aan |
|---|---|---|
| **Oracle Cloud Always Free** (ARM) | gratis | sinds 2026 maximaal **2 cores / 12 GB**. Gebruik het "kleine server"-profiel: 2 streamers tegelijk, GTA VI eerst. Bij aanmaken soms "out of capacity": later opnieuw proberen. |
| **Oude laptop/pc of mini-pc thuis** | gratis / eenmalig ~€150-250 | afhankelijk van de processor; 8 cores = alle 11 streamers |
| **Betaalde VPS** (Hetzner, Contabo, OVH, …) | ~€10-40/mnd | kies 8+ cores en 16 GB voor alle 11 streamers |

Getest: met 4 cores liepen clips achter zodra 5 streamers tegelijk live waren. Reken voor alle
11 streamers dus op minstens 8 cores. Met minder cores werkt het, maar dan alleen met een
beperkt aantal streams tegelijk (zie stap 3).

## 2. Installeren (op de server)

```bash
# Docker installeren (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# Code ophalen
git clone https://github.com/mouniramghar33-cyber/Gta-clipping.git
cd Gta-clipping/autoclipper

# Instellingen
cp .env.example .env
nano .env            # vul minimaal DASHBOARD_PASSWORD in (een lang wachtwoord)
chmod 600 .env
mkdir -p data
```

## 3. Alleen bij een kleine server (Oracle gratis, 2-4 cores)

```bash
cp config.small-server.yaml data/config.yaml
```
Dan worden maximaal 2 streams tegelijk opgenomen (GTA VI gaat altijd voor, daarna de streamers
van wie je clips het best presteren), met een lichter Whisper-model en 30 fps. Later aan te
passen in het dashboard onder Settings → resources.

## 4. Starten

```bash
docker compose up -d --build
docker compose logs -f        # meekijken; Ctrl+C stopt alleen het meekijken
```
Het systeem start daarna ook vanzelf na een herstart of crash.

## 5. HTTPS-adres (nodig voor YouTube/TikTok/Buffer/Kick)

Makkelijkste gratis manier zonder poorten open te zetten: **Cloudflare Tunnel**.
1. Maak een gratis Cloudflare-account en voeg een (goedkoop) domein toe.
2. Zero Trust → Networks → Tunnels → Create tunnel, volg de installatie op de server.
3. Public hostname: bijv. `clips.jouwdomein.nl` → `http://localhost:8080`.
4. Zet in `.env`: `PUBLIC_BASE_URL=https://clips.jouwdomein.nl` en herstart: `docker compose up -d`.

Bij Oracle: zonder tunnel moet je poort 8080/443 openzetten in de "Security List" **én** in de
firewall van de server zelf (`iptables`). De tunnel is eenvoudiger.

## 6. Koppelen (in het dashboard, werkt op je iPhone)

Open `https://clips.jouwdomein.nl`, log in en ga naar **Setup**. Daar staat per stap uitgelegd
wat je moet doen, met de exacte adressen om te kopiëren:

1. **YouTube** – Google Cloud OAuth-client → "Connect YouTube". Zet het toestemmingsscherm op
   *In productie* (anders vervalt de koppeling na 7 dagen).
2. **TikTok automatisch** – gratis Buffer-account + TikTok Business-account → API-sleutel →
   "Import Buffer channels".
3. **YouTube API-sleutel** – voor de chat van IShowSpeed en Typical Gamer.
4. **Kick-app** – voor de chat van Adin Ross, Lacy en Stable Ronaldo op Kick.
5. **Telegram** (optioneel) – meldingen op je telefoon.

Je wachtwoorden van YouTube/TikTok worden nooit gevraagd: je logt in bij de platforms zelf.

## 7. Toestemming en live gaan

1. **Streamers**-tab: vink "Publishing rights" aan voor streamers van wie je toestemming hebt.
2. **Setup → Run dry-run test**: alles wordt gecontroleerd zonder iets te plaatsen.
3. Ziet de wachtrij er goed uit? **Setup → Turn off global dry run**.
4. Noodknop: **■ STOP** bovenaan stopt direct al het plaatsen.

## Updaten

```bash
cd Gta-clipping && git pull && cd autoclipper && docker compose up -d --build
```
