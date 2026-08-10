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

  // Po koľkých minútach bez úspešnej kontroly považovať dáta za staré.
  // MUSÍ sedieť s cronom vo workflowe: ten beží raz týždenne (utorok ráno),
  // takže 8 dní = "vynechal sa celý jeden utorok, niečo je zle". Keby tu
  // zostalo pôvodných 90 minút, stránka by šesť dní v týždni zbytočne
  // strašila, že dáta sú staré. Pri častejšom crone toto číslo zmenš.
  staleAfterMinutes: 8 * 24 * 60,
};
