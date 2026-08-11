// Nastavenia stránky. Meniť sa dá bez zásahu do index.html.
window.CINEMA_CONFIG = {
  // Odkiaľ brať dáta:
  //   1. statusEndpoint — Netlify funkcia, ktorá číta stav priamo z GitHubu
  //      (dáta sú hneď aktuálne, netreba nový deploy). Prázdne = nepoužívať.
  //   2. dataUrl — záložný zdroj, keď funkcia nie je nastavená alebo zlyhá.
  //      Default je súbor nasadený spolu s webom. Dá sa nahradiť aj priamym
  //      odkazom na GitHub, napr.:
  //      "https://raw.githubusercontent.com/meno/repo/master/web/data/status.json"
  statusEndpoint: "/api/status",
  dataUrl: "data/status.json",

  // Netlify funkcia, ktorá spustí kontrolu v GitHub Actions.
  // Prázdne = tlačidlo "Skontrolovať teraz" sa nezobrazí.
  checkEndpoint: "/api/check",

  // Netlify funkcia, ktorá vie sledovanie pozastaviť a znova zapnúť.
  // Prázdne = tlačidlo "Pozastaviť sledovanie" sa nezobrazí.
  toggleEndpoint: "/api/toggle",

  // Ako často si stránka sama znova stiahne dáta (v sekundách; 0 = nikdy).
  refreshSeconds: 60,

  // Pozn.: stránka zámerne nevaruje, že sú dáta staré — v hlavičke je len čas
  // poslednej kontroly a nech si každý sám povie, či mu to stačí. Preto tu už
  // nie je `staleAfterMinutes`; pri zmene cronu netreba nič dolaďovať.
};
