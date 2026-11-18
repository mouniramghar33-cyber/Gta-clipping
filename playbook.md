# Playbook – Faceless GTA 6 clippagina

> De volledige originele prompt staat in `prompt/` (pagina 1 en 2). Dit bestand is de ingevulde, aangepaste versie.
> Werk dit bestand bij na elke wekelijkse review (sectie 13).

## 1. Account

| Veld | Waarde |
|---|---|
| Paginanaam | **[NOG KIEZEN]** – opties: *Vice Chat Clips*, *Chat Lost It: GTA6*, *Leonida Laughs* |
| Handle | **[NOG KIEZEN]** – overal dezelfde, met "GTA6" erin (bv. `@vicechat.gta6`, `@chatlostitgta6`) |
| Platforms | TikTok, YouTube Shorts (accounts bestaan al) |
| Later misschien | Instagram Reels, X |
| Tijdzone | Europe/Amsterdam (CET, UTC+1) |
| Wakker / goedkeuren | 07:00 – 23:00 (soms later) |
| Goedkeuring | Eigenaar keurt **alles** goed voordat het online gaat |
| Tools | Nog te koppelen (scheduler, clipping tool, editor, opslag) |
| Taal content | **Engels** (Amerikaanse streamers = Amerikaans/Engelstalig publiek) |

### Bio TikTok (max. 80 tekens)
```
GTA 6 stream moments when chat loses it 💀
Fan page, not affiliated w/ Rockstar
```

### Bio YouTube (meer ruimte)
```
Funniest GTA 6 stream moments & when chat lost it 💀
Credited clips daily. Fan page, not affiliated with Rockstar Games.
```

### Profielfoto & banner
- Avatar: simpel icoon dat klein leesbaar is, bv. een neon chatbubbel met "💀" of "GTA6" in neon roze/oranje. **Geen** Rockstar-logo of officiële key art.
- Banner (YouTube): neon/zonsondergang, paginanaam + "GTA 6 stream moments when chat goes crazy".

### Pinned post
Na de eerste week: best presterende clip vastpinnen.

## 2. Niche en identiteit

**Hoofdlijn:** grappige GTA 6-momenten uit streams van **Amerikaanse streamers**, vooral de momenten waarop **de chat ontploft** (spam van "💀", "LMAO", "W", "NAHHH", emote-walls).

Formule van elke clip: **moment + reactie streamer + (indien zichtbaar) chat die losgaat**.

## 3. Stem: geen AI-voice-over nodig

We gebruiken **alleen tekst** (hook-tekst + ondertitels) en het originele streamgeluid. Een voice-over is optioneel en voegt hier niets toe: de reactie van de streamer ís de content.

Wel verplicht – "eigen maken" (anders straffen TikTok en YouTube het af als *unoriginal / reused content* en verdien je er later niets aan via monetisatie):
1. Hook-tekst in beeld in de eerste seconde.
2. Ondertitels, groot en gecentreerd.
3. Strak geknipt (geen dode lucht aan het begin, eindigt op de payoff).
4. Verticale framing: gameplay bovenaan, facecam en/of chat eronder (split).
5. Eventueel: zoom op het moment, chat-uitsnede uitvergroot, "chat reaction" label, compilaties (top 3 van de dag).

## 4. Dagelijkse mix (minimaal 5 per platform per dag)

| # | Type | Wat |
|---|---|---|
| 1 | Chat-explosie | Moment waarop chat compleet ontspoort |
| 2 | Fail / NPC-moment | Streamer vs. rare NPC, crash, fysica-fail |
| 3 | Reactie-clip | Grote reactie op iets in de game (eerste keer Vice City zien, enz.) |
| 4 | Chat-explosie of fail | Beste overgebleven clip |
| 5 | Compilatie / "top 3 van gisteren" | 3 korte momenten achter elkaar, 30–50 s |

Kwaliteit boven aantal: liever een slot overslaan dan een zwakke clip posten. Maar mik op 5.

## 5. Posttijden (Amsterdam-tijd)

Amerikaanse streams lopen meestal 's avonds VS-tijd = **nacht in Amsterdam**. Workflow: 's ochtends oogsten, overdag posten richting Amerikaans publiek.

| Slot | Amsterdam | VS Oost (ET) | VS West (PT) |
|---|---|---|---|
| 1 | 13:00 | 07:00 | 04:00 |
| 2 | 16:00 | 10:00 | 07:00 |
| 3 | 19:00 | 13:00 | 10:00 |
| 4 | 21:30 | 15:30 | 12:30 |
| 5 | 23:00 | 17:00 | 14:00 |
| (extra, ingepland) | 01:00 | 19:00 | 16:00 |

- Zonder scheduler: TikTok (via desktop/web) en YouTube Studio kunnen allebei **zelf** video's inplannen. Gebruik dat voor slots na 23:00.
- Na de eerste week passen we de tijden aan op basis van `scoreboard.csv`.

## 6. Dagritme

| Tijd | Wat |
|---|---|
| 07:00–09:00 | **Oogsten**: per streamer in de watchlist de populairste clips van de afgelopen 24 uur bekijken (Twitch: kanaal → Clips → "Last 24 hours", gesorteerd op views) + VOD's van de nacht. Kandidaten in `media/raw/` met bronlink. |
| 09:00–11:00 | **Editen**: 5–7 clips klaarmaken in `media/edits/`. Hook + captions schrijven. |
| 11:00 | **Goedkeuring**: Claude stuurt één bericht met alle clips + exacte captions, jij antwoordt "ja" of past aan. |
| 11:30 | Alles inplannen. |
| Elke 2–3 uur | 15 min engagement (sectie 11). |
| 22:00 | Metrics invullen (12u/24u/72u) in `scoreboard.csv`. |
| 23:00 | Kort dagrapport: wat ging online, beste post, één les, test van morgen. |

## 7. Captions (TikTok + Shorts)

Engels, kort, 3–5 relevante hashtags, credit verplicht.

**Clip:**
```
[One-line hook or reaction] 💀

🎥 @[streamer] on [Twitch/YouTube/Kick]
#gta6 #[streamername] #gta6clips #streamer #fyp
```

**Chat-explosie:**
```
Chat completely lost it 😭

🎥 @[streamer]
#gta6 #[streamername] #twitchclips #gta6funny
```

**Compilatie:**
```
Top 3 GTA 6 stream moments today 💀 which one got you?

🎥 @[streamer1] @[streamer2] @[streamer3]
#gta6 #gta6clips #streamers #funny
```

YouTube Shorts-titel: max. ~60 tekens, hook eerst, bv. `Kai's chat LOST IT at this GTA 6 NPC 💀 #gta6 #shorts`.

## 8. Regels (kort – volledige versie in `prompt/`)

- Altijd credit, in de caption én kort in beeld (`🎥 @streamer`).
- Takedown-verzoek → dezelfde dag verwijderen, loggen in `takedowns.md`, die creator niet meer gebruiken zonder toestemming.
- Geen leaks, pre-launch footage, "play early"-trucs, piraterij of exploits.
- Geen AI-gegenereerde "gameplay". Geen nep-nieuws.
- Nooit doen alsof we Rockstar zijn of iets met de streamer te maken hebben.
- Geen spoilers in hook of thumbnail van verhaalmomenten; story-clips krijgen "SPOILER" in de caption.
- Naaktheid / zware content overslaan of censureren; de game is M-rated.
- Rookie-mistake om te vermijden: kwetsende/haatdragende uitspraken van streamers of chat clippen. Overslaan.

## 9. Specs

- 9:16, 1080×1920, MP4 / H.264 / AAC, 1080p (geen 4K).
- Lengte: 7–30 s standaard, compilaties tot ~50 s. Shorts max. 3 min.
- Ondertitels binnen de safe area (niet onderaan, daar zitten de knoppen van de app).

## 10. Huidige test

Dag 4+: één variabele per dag (zie `learning.md`).
