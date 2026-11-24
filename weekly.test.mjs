import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { join } from 'node:path';
import { webcrypto } from 'node:crypto';

const html = readFileSync(new URL('../outputs/collettore-executive.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const credenziali = new Map();
const localStorage = { getItem: chiave => credenziali.get(chiave) ?? null, setItem: (chiave, valore) => credenziali.set(chiave, valore) };
const app = runInNewContext(script.replace('  schermataIniziale();', `mostraVista = () => {};
return {
  accediTeam, condivisiPMO, streamCondivisiPMO, streamAttivita, vistaSettimanale, filtra, datasetExecutive, avvisiAttivita,
  scegliTeam: indice => { teamSelezionato = indice; teamSbloccati = false; },
  datiExecutive, htmlExecutive, htmlGuidaExecutive, htmlTeam, verificaPasswordTeam, stato, validaDataset, tabelleExport, scriviXlsx, demo: demo_default,
  htmlSettimanale, htmlProdotti, htmlTemi, htmlProgetti, htmlStream, htmlRicerca
};`), { TextEncoder, crypto: webcrypto, localStorage });
const giorno = '2026-11-24';
const dal = '2026-11-17';
const voci = {
  progetto: { tipo: 'PRJ', uid: 'progetto', codice: 'PRJ-1', nome: 'Progetto <Nord>', milestone: [
    { nome: 'Oggi', data: giorno }, { nome: 'Limite', data: '2026-12-24' },
    { nome: 'Fuori', data: '2026-12-25' }, { nome: 'Passata', data: dal }
  ] },
  bloccata: { tipo: 'ATT', uid: 'bloccata', codice: 'ATT-1', titolo: 'Verifica', stato: 'Bloccata', stream: 'HW', scadenza: dal },
  chiusa: { tipo: 'ATT', stato: 'Chiusa', stream: 'HW', scadenza: dal },
  eliminata: { tipo: 'ATT', stato: 'Bloccata', eliminata: true, scadenza: dal },
  oggi: { tipo: 'ATT', stato: 'Aperta', stream: 'HW', scadenza: giorno },
  decisione: { tipo: 'DEC', data: dal },
  futura: { tipo: 'DEC', data: '2026-11-25' }
};
const dataset = { voci, elenchi: { stream: ['HW'] } };

test('L’area Team parte bloccata e verifica la password senza conservarla in chiaro', async () => {
  app.stato.ds = dataset;
  assert.match(app.htmlTeam(), /id="ex-team-login"/);
  assert.doesNotMatch(app.htmlTeam(), /Attività aperte/);
  const salt = Array(16).fill(1);
  const verifica = await app.verificaPasswordTeam('Password di prova', salt);
  assert.match(verifica, /^[0-9a-f]{64}$/);
  assert.equal(await app.verificaPasswordTeam('Password di prova', salt), verifica);
  assert.notEqual(await app.verificaPasswordTeam('Password diversa', salt), verifica);
});

test('Ogni team ha un accesso distinto e rifiuta la password di un altro team', async () => {
  app.stato.ds = { ...dataset, elenchi: { stream: ['HW', 'FW'] } };
  app.stato.vista = 'team';
  async function accedi(password) {
    const errore = {};
    const pulsante = {};
    const modulo = { id: 'ex-team-login', isConnected: true, reset() {},
      elements: { password: { value: password }, conferma: { value: password } },
      querySelector: selettore => selettore === 'button' ? pulsante : errore };
    await app.accediTeam({ target: modulo, preventDefault() {} });
    return errore.textContent;
  }
  app.scegliTeam(0);
  assert.equal(await accedi('Password HW di prova'), undefined);
  assert.match(app.htmlTeam(), /Attività aperte/);
  assert.match(app.htmlTeam(), /id="ex-team-new"/);
  app.scegliTeam(1);
  assert.doesNotMatch(app.htmlTeam(), /Attività aperte/);
  assert.match(await accedi('Password HW di prova'), /già in uso/);
  assert.equal(await accedi('Password FW di prova'), undefined);
  app.scegliTeam(0);
  assert.match(await accedi('Password FW di prova'), /errata/);
  assert.equal(await accedi('Password HW di prova'), undefined);
  assert.equal(credenziali.size, 2);
});

test('Un’attività creata nell’Area Team entra in Executive e Staff solo se condivisa', () => {
  const voce = { ...voci.bloccata, soloAreaTeam: true, streamAggiuntivi: ['FW'] };
  const nota = { tipo: 'AGG', uid: 'nota', rif: voce.uid, data: giorno };
  const archivio = { ...dataset, elenchi: { stream: ['HW', 'FW'] }, voci: { ...voci, bloccata: voce, nota } };
  assert.equal(app.datasetExecutive(archivio).voci.bloccata, undefined);
  assert.equal(app.datasetExecutive(archivio).voci.nota, undefined);
  assert.equal(app.datasetExecutive(archivio).voci.oggi, voci.oggi);
  assert.equal(archivio.voci.bloccata, voce);
  voce.condivisionePMO = ['FW'];
  assert.equal(app.datasetExecutive(archivio).voci.bloccata, voce);
  voce.condivisionePMO = [];
  assert.equal(app.datasetExecutive(archivio).voci.bloccata, undefined);
});

test('Il warning segue i team dell’attività e la sua visibilità PMO', () => {
  const voce = { ...voci.bloccata, soloAreaTeam: true, streamAggiuntivi: ['FW'], warningAttivita: 'Verifica <EMC>' };
  const archivio = { ...dataset, elenchi: { stream: ['HW', 'FW'] }, voci: { bloccata: voce } };
  assert.equal(app.avvisiAttivita(archivio, 'FW').length, 1);
  assert.equal(app.avvisiAttivita(archivio, 'QA').length, 0);
  assert.equal(app.avvisiAttivita(app.datasetExecutive(archivio)).length, 0);
  voce.condivisionePMO = ['FW'];
  app.stato.vista = 'executive';
  assert.match(app.htmlExecutive(archivio, giorno), /Verifica &lt;EMC&gt;/);
  voce.stato = 'Chiusa';
  assert.equal(app.avvisiAttivita(archivio, 'FW').length, 0);
});

test('KPI, milestone e periodo rispettano date e record eliminati', () => {
  const prima = JSON.stringify(dataset);
  const dati = app.datiExecutive(dataset, giorno, dal);
  assert.equal(dati.aperte.length, 2);
  assert.equal(dati.bloccate.length, 1);
  assert.equal(dati.scadute.length, 1);
  assert.equal(dati.decisioni.length, 1);
  assert.equal(dati.milestone.map(voce => voce.nome).join(','), 'Oggi,Limite');
  assert.equal(JSON.stringify(dataset), prima);
});

test('Executive mostra KPI, link alle schede ed escape dei dati', () => {
  app.stato.dal = dal;
  const vista = app.htmlExecutive(dataset, giorno);
  assert.match(vista, /data-ex-focus="bloccate"[^>]*><strong>1/);
  assert.match(vista, /data-apri="progetto"/);
  assert.match(vista, /Progetto &lt;Nord&gt;/);
  assert.match(vista, /data-vista="meeting"/);
  app.stato.vista = 'meeting';
  const meeting = app.htmlExecutive(dataset, giorno);
  assert.match(meeting, /1 \/ 8/);
  assert.match(meeting, /Condiviso dai team/);
  assert.match(meeting, /Per stream/);
  assert.match(meeting, /Aggiungi nota/);
  assert.match(meeting, /Esci dal meeting/);
  assert.doesNotMatch(meeting, /Milestone nei prossimi 30 giorni/);
});

test('Il riepilogo PMO include solo le voci selezionate da stream validi', () => {
  const voce = { ...voci.bloccata, condivisionePMO: ['HW', 'FW'] };
  const condivise = { ...dataset, voci: { ...voci, bloccata: voce,
    eliminata: { ...voce, eliminata: true } } };
  assert.equal(app.condivisiPMO(condivise).map(record => record.uid).join(','), 'bloccata');
  assert.equal(app.streamCondivisiPMO(condivise, voce).join(','), 'HW');
  const prima = JSON.stringify(condivise);
  app.stato.vista = 'executive';
  assert.match(app.htmlExecutive(condivise, giorno), /Condiviso dai team<\/h2><ul[^]*?data-apri="bloccata"/);
  assert.equal(JSON.stringify(condivise), prima);
  voce.condivisionePMO = [];
  assert.equal(app.condivisiPMO(condivise).length, 0);
});

test('Un’attività appare in più team e una volta sola nel riepilogo PMO', () => {
  const voce = { ...voci.bloccata, streamAggiuntivi: ['FW', 'HW', 'FW'], condivisionePMO: ['HW', 'FW'] };
  const archivio = { ...dataset, elenchi: { stream: ['HW', 'FW'] }, voci: { bloccata: voce } };
  assert.equal(app.streamAttivita(voce).join(','), 'HW,FW');
  assert.equal(app.streamAttivita(voci.bloccata).join(','), 'HW');
  assert.equal(app.vistaSettimanale(archivio, { oggi: giorno, dal }).map(riga => riga.bloccate.length).join(','), '1,1');
  assert.equal(app.filtra(Object.values(archivio.voci), { stream: 'FW' }).length, 1);
  assert.equal(app.datiExecutive(archivio, giorno, dal).aperte.length, 1);
  assert.equal(app.condivisiPMO(archivio).length, 1);
  assert.equal(app.streamCondivisiPMO(archivio, voce).join(','), 'HW,FW');
  voce.condivisionePMO = ['FW'];
  assert.equal(app.condivisiPMO(archivio).length, 1);
});

test('Le note collegate compaiono nella vista Note e nello stream associato', () => {
  app.stato.vista = 'executive';
  const nota = { uid: 'nota', codice: 'AGG-1', tipo: 'AGG', notaStaff: true, rif: 'bloccata', data: giorno, testo: 'Rivedere il piano' };
  const conNota = JSON.parse(JSON.stringify({ ...dataset, voci: { ...voci, nota } }));
  const vista = app.htmlExecutive(conNota, giorno);
  assert.match(vista, /1 aperte · 1 bloccate · 1 scadute · 1 note|2 aperte · 1 bloccate · 1 scadute · 1 note/);
  assert.match(vista, /Note del periodo/);
  assert.match(vista, /data-apri="nota"/);
  assert.match(vista, /Rivedere il piano/);
});

test('I warning riusano i rischi alti e la guida è collegata alla dashboard', () => {
  const rischio = { uid: 'rischio', tipo: 'RIS', codice: 'RIS-1', testo: 'Banco indisponibile', impatto: 'Alto', data: giorno };
  const conRischi = { ...dataset, voci: { ...voci, rischio, medio: { ...rischio, impatto: 'Medio' }, eliminato: { ...rischio, eliminata: true } } };
  assert.equal(app.datiExecutive(conRischi, giorno, dal).warning.length, 1);
  app.stato.vista = 'executive';
  assert.match(app.htmlExecutive(conRischi, giorno), /Warning <span>1<\/span>/);
  assert.match(app.htmlExecutive(conRischi, giorno), /data-nuovo="RIS" data-ex-warning/);
  assert.match(app.htmlGuidaExecutive(), /data-vista="executive"/);
  assert.match(html, /apriForm\("RIS", \{ impatto: "Alto", data: oggi\(\) \}\)/);
});

test('Il dataset demo continua a supportare le viste precedenti e l’export', () => {
  const opzioni = { oggi: giorno, dal };
  const demo = app.demo;
  const prodotto = Object.values(demo.voci).find(voce => voce.tipo === 'PRD');
  for (const vista of [
    app.htmlSettimanale(demo, opzioni), app.htmlProdotti(demo, prodotto.uid, opzioni),
    app.htmlTemi(demo), app.htmlProgetti(demo, opzioni), app.htmlStream(demo, opzioni),
    app.htmlRicerca(demo, {}, opzioni)
  ]) assert.ok(vista.length > 100);
  assert.ok(Object.keys(app.tabelleExport(demo)).length > 0);
  assert.equal(Buffer.from(app.scriviXlsx(app.tabelleExport(demo))).subarray(0, 4).toString('hex'), '504b0304');
});

test('Gli archivi JSON esistenti sono leggibili e alimentano la dashboard', () => {
  app.stato.vista = 'executive';
  for (const cartella of ['PMO', 'collettore-sorgenti']) {
    const percorso = join(process.env.USERPROFILE, 'Downloads', cartella, 'collettore.json');
    const archivio = JSON.parse(readFileSync(percorso, 'utf8').replace(/^\uFEFF/, ''));
    assert.equal(app.validaDataset(archivio).ok, true);
    assert.match(app.htmlExecutive(archivio, giorno), /Weekly Executive Review/);
  }
});
