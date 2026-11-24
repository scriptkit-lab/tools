import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const originale = readFileSync(join(process.env.USERPROFILE, 'Downloads', 'collettore.html'), 'utf8');
const leggi = nome => readFileSync(new URL(nome, import.meta.url), 'utf8');
let html = originale;
function inserisci(ancora, contenuto) {
  assert.equal(html.split(ancora).length, 2, ancora);
  html = html.replace(ancora, () => contenuto);
}
inserisci('</style>', leggi('weekly.css') + '</style>');
inserisci('["stream", "Stream", "elenco:stream", 1]', '["stream", "Team principale", "elenco:stream", 1],\n      ["streamAggiuntivi", "Altri team coinvolti", "multistream"],\n      ["warningAttivita", "Warning (vuoto per rimuoverlo)", "area"]');
inserisci('        inElenco("stream", el.stream, "stream", true);', `        inElenco("stream", el.stream, "stream", true);
        if (voce.streamAggiuntivi !== undefined && (!Array.isArray(voce.streamAggiuntivi) ||
          voce.streamAggiuntivi.some(nome => !el.stream.includes(nome)))) {
          e.push("Altri team coinvolti: seleziona team presenti nell’elenco.");
        }`);
inserisci('    if (v.tipo === "ATT") return [v.stream];', '    if (v.tipo === "ATT") return streamAttivita(v);');
inserisci('  function streamDi(ds, v) {', `  function streamAttivita(voce) {
    const altri = Array.isArray(voce.streamAggiuntivi) ? voce.streamAggiuntivi : [];
    return [...new Set([voce.stream, ...altri].filter(Boolean))];
  }
  function streamDi(ds, v) {`);
html = html.replaceAll('a.stream === s &&', 'streamAttivita(a).includes(s) &&');
inserisci('    for (const a of att) (attivitaPerStream[a.stream] ??= []).push(a);', '    for (const a of att) {\n      for (const nome of streamAttivita(a)) (attivitaPerStream[nome] ??= []).push(a);\n    }');
inserisci('att.filter((x) => x.stream === stream)', 'att.filter((x) => streamAttivita(x).includes(stream))');
inserisci('!(v.stream === stream || comprende(v.stream, stream))', '!(v.tipo === "ATT" ? streamAttivita(v).includes(stream) : v.stream === stream || comprende(v.stream, stream))');
inserisci('["Stream", (v) => v.stream]', '["Stream", (v) => v.stream],\n        ["Altri team coinvolti", (v) => streamAttivita(v).filter(nome => nome !== v.stream).join(", ")]');
inserisci('    r("Stream", esc2(Array.isArray(v.stream) ? v.stream.join(", ") : v.stream));', '    r(v.tipo === "ATT" ? "Team principale" : "Stream", esc2(Array.isArray(v.stream) ? v.stream.join(", ") : v.stream));\n    if (v.tipo === "ATT") r("Altri team coinvolti", esc2(streamAttivita(v).filter(nome => nome !== v.stream).join(", ")));');
inserisci('    r("Stato", badgeStato(v.stato));', '    r("Stato", badgeStato(v.stato));\n    if (v.tipo === "ATT") r("Warning", esc2(v.warningAttivita));');
inserisci('    $("#vista").innerHTML = html;\n    aggiornaBarra();', '    $("#vista").innerHTML = html;\n    aggiornaBarra();\n    controllaAvvisiAttivita();');
inserisci('  var VISTE = [[', leggi('weekly.js') + '\n  var VISTE = [["executive", "Executive"], ["team", "Area Team"], ["guida", "Guida"], [');
inserisci('vista: "settimanale"', 'vista: "executive"');
inserisci('    collegaEventi();', '    collegaEventi();\n    collegaExecutive();');
inserisci('    const html = {\n      settimanale:', '    document.body.classList.toggle("pmo-meeting", stato.vista === "meeting");\n    const html = {\n      executive: () => htmlExecutive(stato.ds, o.oggi),\n      meeting: () => htmlExecutive(stato.ds, o.oggi),\n      team: htmlTeam,\n      guida: htmlGuidaExecutive,\n      settimanale:');
inserisci('      else if (ds.nuovo) apriForm(ds.nuovo);', '      else if ("exWarning" in ds) apriForm("RIS", { impatto: "Alto", data: oggi() });\n      else if (ds.nuovo) apriForm(ds.nuovo);');
inserisci('    stato.vista = vista;', `    if (vista !== "team") teamSbloccati = false;
    if (vista === "meeting") {
      paginaExecutive = 0;
      focusExecutive = "";
      window.scrollTo(0, 0);
    }
    if (stato.vista === "meeting" && vista !== "meeting" && document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
    stato.vista = vista;`);
inserisci('<title>Collettore Ingegneria</title>', '<title>Engineering Control Tower · Collettore</title>');
writeFileSync(new URL('../outputs/collettore-executive.html', import.meta.url), html);
