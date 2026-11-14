## Soundbite

A browser-based party game where players race to identify a song from a short
soundbite. The first correct answer earns 100 points, with later correct
answers earning 80, 60, and so on (down to 40).

### Run locally

1. Install Python 3.10 or newer.
2. Start the server:

   ```text
   python main.py
   ```

3. Open `http://localhost:8000`, create a lobby, and share the four-digit code.
4. As host, choose a track from the built-in bread library before starting
   each round. Players only see the soundbite and answer box.

The static Pages demo uses official YouTube embeds for curated playlists of
recognizable songs from the 90s, 2000s, 2010s, and EDM. The host chooses only a
playlist; each round randomly selects the song. YouTube playback stays inside
the visible player; the app does not download, extract, or re-host copyrighted
audio.

### GitHub Pages demo

`index.html` is a dependency-free static demo suitable for GitHub Pages. It
has the same host, join, library, YouTube playback, and scoring flow, but state
is local to the current browser. Real multiplayer across devices still
requires the Python server in `main.py` (or a hosted backend).

The static frontend is split by responsibility:

- `index.html` — page structure and accessible form controls
- `styles.css` — bakery theme and responsive layout
- `game-data.js` — curated playlist and YouTube metadata
- `app.js` — lobby, round, buzzer, scoring, and player interactions

## Authors

Contributors names and contact info

[Richard Okyne Ampah](https://www.linkedin.com/in/richard-okyne-ampah-01001538a/)

[Aakanksh Prabhu](https://www.linkedin.com/in/aakanksh-prabhu-8b92bb442/)

[Benjamin Woolsey](https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcR11j5NlMxitnCSX23L_S0tE8QeFYDtvKsmaL-YwNvLOvGXzaE3Rm2Q_vIdEpb_8bsGzaprpiSeG0ofpJM2ie9yjea1NDnK4xA7Aq3bww&s=10)

[Danny Jordan](https://www.linkedin.com/in/thedannyjordan/)


## License

This project is licensed under the MIT License - see the LICENSE.md file for details
