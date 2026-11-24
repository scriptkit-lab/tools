  function datiExecutive(dataset, giorno, dal) {
    const attivita = vive2(dataset, "ATT");
    const aperte = attivita.filter(voce => voce.stato !== "Chiusa");
    const progetti = vive2(dataset, "PRJ");
    const rischi = vive2(dataset, "RIS").sort(perDataExecutive);
    const fine = aggiungiGiorni(giorno, 30);
    const nelPeriodo = voce => voce.data >= dal && voce.data <= giorno;
    return {
      aperte, progetti, attivita,
      bloccate: aperte.filter(voce => voce.stato === "Bloccata"),
      scadute: aperte.filter(voce => voce.scadenza && voce.scadenza < giorno)
        .sort((prima, seconda) => prima.scadenza.localeCompare(seconda.scadenza)),
      rischi, warning: rischi.filter(voce => voce.impatto === "Alto"),
      decisioni: vive2(dataset, "DEC").filter(nelPeriodo).sort(perDataExecutive),
      aggiornamenti: vive2(dataset, "AGG").filter(nelPeriodo).sort(perDataExecutive),
      milestone: progetti.flatMap(progetto => (progetto.milestone ?? [])
        .filter(voce => voce.data >= giorno && voce.data <= fine)
        .map(voce => ({ ...voce, progetto })))
        .sort((prima, seconda) => prima.data.localeCompare(seconda.data))
    };
  }
  const perDataExecutive = (prima, seconda) => (seconda.data ?? "").localeCompare(prima.data ?? "");
  let paginaExecutive = 0;
  let focusExecutive = "";
  const prefissoPasswordTeam = "collettore.team.password.v2:";
  let teamSbloccati = false;
  let teamSelezionato = 0;
  const chiavePasswordTeam = nome => prefissoPasswordTeam + nome;
  function leggiPasswordTeam(nome) {
    try {
      return JSON.parse(localStorage.getItem(chiavePasswordTeam(nome)));
    } catch {
      return null;
    }
  }
  async function verificaPasswordTeam(password, salt) {
    const chiave = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
    const bytes = await crypto.subtle.deriveBits({
      name: "PBKDF2", hash: "SHA-256", iterations: 600000, salt: Uint8Array.from(salt)
    }, chiave, 256);
    return Array.from(new Uint8Array(bytes), valore => valore.toString(16).padStart(2, "0")).join("");
  }
  function streamCondivisiPMO(dataset, voce) {
    const riferimento = voce.tipo === "AGG" ? viva(dataset, voce.rif) : voce;
    return (voce.condivisionePMO ?? []).filter(nome =>
      dataset.elenchi.stream.includes(nome) && streamDi(dataset, riferimento).includes(nome));
  }
  function condivisiPMO(dataset) {
    return vive2(dataset).filter(voce => streamCondivisiPMO(dataset, voce).length > 0).sort(perCodice);
  }
  function datasetExecutive(dataset) {
    const riservataAlTeam = voce => voce?.soloAreaTeam === true && !streamCondivisiPMO(dataset, voce).length;
    return { ...dataset, voci: Object.fromEntries(Object.entries(dataset.voci).filter(([, voce]) =>
      !riservataAlTeam(voce) && !(voce.tipo === "AGG" && riservataAlTeam(dataset.voci[voce.rif])))) };
  }
  const avvisiAttivitaVisti = new Set();
  function avvisiAttivita(dataset, stream) {
    return vive2(dataset, "ATT").filter(voce => voce.stato !== "Chiusa" &&
      typeof voce.warningAttivita === "string" && voce.warningAttivita.trim() &&
      (!stream || streamAttivita(voce).includes(stream))).sort(perCodice);
  }
  function avvisiVistaAttuale() {
    if (stato.vista === "team" && teamSbloccati) {
      const nome = stato.ds.elenchi.stream[teamSelezionato];
      return nome ? avvisiAttivita(stato.ds, nome) : [];
    }
    return stato.vista === "executive" ? avvisiAttivita(datasetExecutive(stato.ds)) : [];
  }
  function listaAvvisiAttivita(voci) {
    return `<ul class="voci pmo-avvisi">${voci.map(voce => `<li>${link(voce)}<p>${esc2(voce.warningAttivita)}</p></li>`).join("")}</ul>`;
  }
  function mostraAvvisiAttivita(voci) {
    if (!voci.length) return;
    apriFinestra(`<section class="pmo-panel pmo-warning"><h2>Warning sulle attività</h2>${listaAvvisiAttivita(voci)}
      <div class="azioni-form"><button class="primario" data-chiudi>Chiudi avviso</button></div></section>`);
  }
  function controllaAvvisiAttivita() {
    if ($("#finestra").open) return;
    const ambito = stato.vista === "team" ? ["team", stato.ds.elenchi.stream[teamSelezionato]] : [stato.vista];
    const nuovi = avvisiVistaAttuale().filter(voce => {
      const chiave = JSON.stringify([...ambito, voce.uid, voce.warningAttivita]);
      if (avvisiAttivitaVisti.has(chiave)) return false;
      avvisiAttivitaVisti.add(chiave);
      return true;
    });
    mostraAvvisiAttivita(nuovi);
  }
  function pulsanteAvvisiAttivita(voci) {
    return voci.length ? `<button type="button" id="ex-alerts" class="pmo-alert-button">Warning attività (${voci.length})</button>` : "";
  }
  function aggiornaCondivisionePMO(uid) {
    if (stato.vista !== "team" || !teamSbloccati) return;
    const dataset = stato.ds;
    const nome = dataset.elenchi.stream[teamSelezionato];
    const voce = viva(dataset, uid);
    const riferimento = voce?.tipo === "AGG" ? viva(dataset, voce.rif) : voce;
    if (!nome || !voce || !streamDi(dataset, riferimento).includes(nome)) return;
    const stream = new Set(streamCondivisiPMO(dataset, voce));
    if (stream.has(nome)) stream.delete(nome);
    else stream.add(nome);
    dataset.voci[uid] = aggiornaVoce(voce, { condivisionePMO: [...stream] }, autore(), new Date());
    segnaModificato();
    Array.from(document.querySelectorAll("[data-ex-pmo]")).find(pulsante => pulsante.dataset.exPmo === uid)?.focus();
  }
  function htmlTeam() {
    const dataset = stato.ds;
    const stream = dataset.elenchi.stream;
    const nome = stream[teamSelezionato];
    const navigazione = `<nav class="pmo-deck" aria-label="Team">${stream.map((voce, indice) => `<button data-ex-team="${indice}" aria-current="${indice === teamSelezionato ? "page" : "false"}">${esc2(voce)}</button>`).join("")}</nav>`;
    if (!nome) return '<div class="pmo"><section class="pmo-panel"><h1>Area Team</h1><p>Nessun team configurato.</p></section></div>';
    if (!teamSbloccati) {
      const configurata = Boolean(leggiPasswordTeam(nome));
      return `<div class="pmo"><header class="pmo-hero"><div><p class="pmo-eyebrow">AREA TEAM</p><h1>${esc2(nome)}</h1></div></header>${navigazione}
        <form class="pmo-panel pmo-accesso" id="ex-team-login"><h2>${configurata ? "Sblocca area Team" : "Imposta la password"}</h2>
        <p>Usa una password dedicata a questo team. La password vale in questo browser. Le autorizzazioni alla cartella regolano l’accesso ai dati.</p>
        <label>Password <input name="password" type="password" autocomplete="${configurata ? "current-password" : "new-password"}" ${configurata ? "" : 'minlength="10"'} required></label>
        ${configurata ? "" : '<label>Conferma password <input name="conferma" type="password" autocomplete="new-password" required></label>'}
        <button class="primario">${configurata ? "Sblocca" : "Imposta e accedi"}</button><p role="alert"></p></form></div>`;
    }
    const giorno = oggi();
    const riguarda = voce => Boolean(nome) && streamDi(dataset, voce).includes(nome);
    const dati = datiExecutive(dataset, giorno, stato.dal);
    const note = dati.aggiornamenti.filter(voce => voce.notaStaff && riguarda(viva(dataset, voce.rif)));
    const attivita = dati.attivita.filter(riguarda);
    const pannello = (titolo, contenuto) => `<section class="pmo-panel"><h2>${titolo}</h2>${contenuto}</section>`;
    const elenco = voci => voci.length ? `<ul class="voci pmo-team-voci">${voci.map(voce => {
      const condivisa = streamCondivisiPMO(dataset, voce).includes(nome);
      return `<li class="riga-voce"><div>${link(voce)} ${badgeStato(voce.stato)}
        <span class="meta">${esc2(voce.owner || voce.autore || "")} ${dataIt(voce.scadenza ?? voce.data)}
        ${voce.tipo === "AGG" ? link(viva(dataset, voce.rif)) : ""}</span></div>
        <button type="button" data-ex-pmo="${esc2(voce.uid)}" aria-pressed="${condivisa}">${condivisa ? "Rimuovi dal PMO" : "Condividi con PMO"}</button></li>`;
    }).join("")}</ul>` : '<p class="vuoto">Nessuna voce</p>';
    const condivisePassate = condivisiPMO(dataset).filter(voce =>
      streamCondivisiPMO(dataset, voce).includes(nome) &&
      ((voce.tipo === "DEC" && !dati.decisioni.includes(voce)) || (voce.tipo === "AGG" && !note.includes(voce))));
    return `<div class="pmo"><header class="pmo-hero"><div><p class="pmo-eyebrow">AREA TEAM</p><h1>${esc2(nome ?? "Team")}</h1></div>
      <div class="pmo-controls"><button id="ex-team-new">Nuova attività</button>${pulsanteAvvisiAttivita(avvisiAttivita(dataset, nome))}${stato.modo !== "demo" && stato.modificato ? '<button id="ex-save">Salva archivio</button>' : ""}<button id="ex-team-lock">Blocca area</button></div></header>
      ${navigazione}
      <p class="pmo-panel">Scegli quali voci inserire in <strong>Condiviso dai team</strong>. Le selezioni si aggiornano subito in questo collettore. Salva l’archivio per renderle disponibili a chi lo usa.</p>
      <div class="pmo-grid">${pannello("Attività aperte", elenco(attivita.filter(voce => voce.stato !== "Chiusa")))}
      ${pannello("Attività chiuse", elenco(attivita.filter(voce => voce.stato === "Chiusa")))}
      ${pannello("Temi del team", elenco(vive2(dataset, "TEM").filter(riguarda)))}
      ${pannello("Warning", elenco(dati.warning.filter(riguarda)))}
      ${pannello("Decisioni del periodo", elenco(dati.decisioni.filter(riguarda)))}
      ${pannello("Note del periodo", elenco(note))}
      ${condivisePassate.length ? pannello("Condivisioni fuori periodo", elenco(condivisePassate)) : ""}</div></div>`;
  }
  async function accediTeam(evento) {
    const modulo = evento.target;
    if (modulo.id !== "ex-team-login") return;
    evento.preventDefault();
    const errore = modulo.querySelector('[role="alert"]');
    const pulsante = modulo.querySelector("button");
    const password = modulo.elements.password.value;
    const nome = stato.ds.elenchi.stream[teamSelezionato];
    const salvata = leggiPasswordTeam(nome);
    if (!salvata && password !== modulo.elements.conferma?.value) {
      errore.textContent = "Le password non coincidono.";
      return;
    }
    pulsante.disabled = true;
    try {
      const salt = salvata?.salt ?? Array.from(crypto.getRandomValues(new Uint8Array(16)));
      const verifica = await verificaPasswordTeam(password, salt);
      if (stato.vista !== "team" || !modulo.isConnected) return;
      if (salvata && salvata.verifica !== verifica) {
        errore.textContent = "Password errata.";
        return;
      }
      if (!salvata) {
        for (const altroTeam of stato.ds.elenchi.stream) {
          if (altroTeam === nome) continue;
          const altra = leggiPasswordTeam(altroTeam);
          if (altra && await verificaPasswordTeam(password, altra.salt) === altra.verifica) {
            errore.textContent = "Questa password è già in uso per un altro team.";
            return;
          }
        }
        if (stato.vista !== "team" || !modulo.isConnected) return;
        localStorage.setItem(chiavePasswordTeam(nome), JSON.stringify({ salt, verifica }));
      }
      teamSbloccati = true;
      mostraVista();
    } catch {
      errore.textContent = "Impossibile usare la password in questo browser.";
    } finally {
      pulsante.disabled = false;
      modulo.reset();
    }
  }
  const sezioniExecutive = ["Sintesi", "Condiviso dai team", "Warning", "Criticità", "Per stream", "Roadmap", "Aggiornamenti e decisioni", "Note"];
  function htmlNoteStaff(dataset, note) {
    return note.length ? `<ul class="voci">${note.map(voce =>
      `<li class="riga-voce">${link(voce)} <span class="meta">${dataIt(voce.data)} · ${esc2(voce.autore || voce.creataDa || "")} · ${link(viva(dataset, voce.rif))}</span></li>`).join("")}</ul>` : '<p class="vuoto">Nessuna nota nel periodo selezionato.</p>';
  }
  function apriNotaStaff() {
    const riferimenti = vive2(stato.ds).filter(voce => voce.tipo !== "AGG").sort(perCodice);
    if (!riferimenti.length) return messaggio("Crea una scheda a cui collegare la nota.", "avviso");
    const opzioni = riferimenti.map(voce => `<option value="${esc2(voce.uid)}">${esc2(voce.codice)} · ${esc2(voce.titolo ?? voce.nome ?? voce.testo ?? "")}</option>`).join("");
    const dialogo = apriFinestra(`<form class="voce"><h2>Aggiungi nota</h2>
      <div class="errori" role="alert"></div>
      <div class="griglia-campi"><label>Scheda di riferimento <select name="rif" required><option value="">Seleziona una scheda</option>${opzioni}</select></label>
      <label>Nota <textarea name="testo" required rows="5"></textarea></label></div>
      <div class="azioni-form"><button type="submit" class="primario">Registra nota</button><button type="button" data-annulla>Annulla</button></div></form>`);
    const modulo = $("form.voce", dialogo);
    modulo.addEventListener("submit", evento => {
      evento.preventDefault();
      const campi = leggiForm(modulo, "AGG");
      campi.rif = modulo.elements.rif.value;
      campi.data = oggi();
      campi.autore = autore();
      campi.notaStaff = true;
      const nota = creaVoce(stato.ds, "AGG", campi, campi.autore, new Date());
      const errori = validaVoce(stato.ds, nota);
      if (errori.length) return mostraErrori(modulo, errori);
      stato.ds.voci[nota.uid] = nota;
      chiudiFinestra();
      segnaModificato();
      messaggio("Nota registrata. Salva l’archivio per conservarla.");
    });
  }
  function htmlExecutive(dataset, giorno) {
    dataset = datasetExecutive(dataset);
    const avvisi = avvisiAttivita(dataset);
    const dati = datiExecutive(dataset, giorno, stato.dal);
    const meeting = stato.vista === "meeting";
    const elenco = voci => lista(dataset, voci, "Nessuna voce", { oggi: giorno });
    const pannello = (titolo, corpo) => `<section class="pmo-panel"><h2>${titolo}</h2>${corpo}</section>`;
    const kpi = (chiave, titolo, voci, classe = "") =>
      `<button class="pmo-kpi ${classe}" data-ex-focus="${chiave}" aria-pressed="${focusExecutive === chiave}"><strong>${voci.length}</strong><span>${titolo}</span></button>`;
    const titoli = { progetti: "Progetti in archivio", aperte: "Attività aperte", bloccate: "Attività bloccate", scadute: "Attività scadute" };
    const indicatori = `<div class="pmo-kpis">${Object.entries(titoli).map(([chiave, titolo]) => kpi(chiave, titolo, dati[chiave], ["bloccate", "scadute"].includes(chiave) && dati[chiave].length ? "attenzione" : "")).join("")}</div>`;
    const stream = dataset.elenchi.stream.map(nome => {
      const voci = dati.attivita.filter(voce => streamAttivita(voce).includes(nome));
      const chiuse = voci.filter(voce => voce.stato === "Chiusa").length;
      return `<details class="pmo-stream"><summary><strong>${esc2(nome)}</strong><progress aria-label="Attività chiuse" value="${chiuse}" max="${voci.length || 1}"></progress><span>${chiuse}/${voci.length} chiuse</span></summary>${elenco(voci)}</details>`;
    }).join("");
    const roadmap = dati.milestone.length ? `<ol class="pmo-roadmap">${dati.milestone.map(voce =>
      `<li><time datetime="${esc2(voce.data)}">${dataIt(voce.data)}</time><div><strong>${esc2(voce.nome)}</strong><div>${link(voce.progetto)}</div></div></li>`).join("")}</ol>` : '<p class="vuoto">Nessuna milestone pianificata nei prossimi 30 giorni.</p>';
    const warning = `<section class="pmo-panel pmo-warning"><div class="pmo-warning-header">
      <h2>Warning <span>${dati.warning.length}</span></h2>
      <button type="button" data-nuovo="RIS" data-ex-warning>Aggiungi warning</button>
      </div>${dati.warning.length ? `<ul class="pmo-warning-list">${dati.warning.map(voce =>
        `<li>${link(voce)}<div class="meta">${esc2(voce.owner || "Responsabile da assegnare")} · ${dataIt(voce.data)}${voce.tema && dataset.voci[voce.tema] ? ` · ${link(dataset.voci[voce.tema])}` : ""}</div></li>`).join("")}</ul>` : '<p class="vuoto">Nessun warning registrato.</p>'}</section>`;
    const note = dati.aggiornamenti.filter(voce => voce.notaStaff === true);
    const perStream = dataset.elenchi.stream.map(nome => {
      const appartiene = voce => streamDi(dataset, voce).includes(nome);
      const attivita = dati.aperte.filter(appartiene);
      const bloccate = dati.bloccate.filter(appartiene).length;
      const scadute = dati.scadute.filter(appartiene).length;
      const noteStream = note.filter(voce => appartiene(viva(dataset, voce.rif)));
      return `<details class="pmo-panel pmo-stream-card"><summary><strong>${esc2(nome)}</strong><span>${attivita.length} aperte · ${bloccate} bloccate · ${scadute} scadute · ${noteStream.length} note</span></summary>
      <h3>Attività aperte</h3>${elenco(attivita)}<h3>Note del periodo</h3>${htmlNoteStaff(dataset, noteStream)}</details>`;
    }).join("");
    const condivise = condivisiPMO(dataset);
    const condivisioni = condivise.length ? `<ul class="voci">${condivise.map(voce =>
      `<li class="riga-voce">${link(voce)} ${badgeStato(voce.stato)} <span class="meta">${esc2(streamCondivisiPMO(dataset, voce).join(" · "))}${voce.scadenza || voce.data ? ` · ${dataIt(voce.scadenza ?? voce.data)}` : ""}</span></li>`).join("")}</ul>`
      : '<p class="vuoto">Nessuna voce condivisa dai team.</p>';
    const contenuti = [
      indicatori + (focusExecutive ? pannello(titoli[focusExecutive], elenco(dati[focusExecutive])) : "") + pannello("Attività chiuse per stream", stream || '<p class="vuoto">Nessuno stream configurato.</p>'),
      pannello("Condiviso dai team", condivisioni),
      warning + (avvisi.length ? pannello("Warning sulle attività", listaAvvisiAttivita(avvisi)) : ""),
      `<div class="pmo-grid">${pannello("Attività bloccate", elenco(dati.bloccate))}${pannello("Attività scadute", elenco(dati.scadute))}${pannello("Rischi registrati", elenco(dati.rischi))}</div>`,
      perStream ? `<div class="pmo-stream-grid">${perStream}</div>` : pannello("Stream", '<p class="vuoto">Nessuno stream configurato.</p>'),
      pannello("Milestone nei prossimi 30 giorni", roadmap),
      `<div class="pmo-grid">${pannello("Aggiornamenti del periodo", elenco(dati.aggiornamenti))}${pannello("Decisioni del periodo", elenco(dati.decisioni))}</div>`,
      pannello("Note del periodo", htmlNoteStaff(dataset, note))
    ];
    return `<div class="pmo">
      <header class="pmo-hero">
        <div><p class="pmo-eyebrow">${meeting ? "STAFF MEETING SETTIMANALE" : "ENGINEERING CONTROL TOWER"}</p><h1>${meeting ? sezioniExecutive[paginaExecutive] : "Weekly Executive Review"}</h1>
        <p>${dataIt(stato.dal)} – ${dataIt(giorno)}${stato.modo === "demo" ? " · Dati demo" : ""}</p></div>
        <div class="pmo-controls">${meeting
          ? `<button id="ex-full">Schermo intero</button><button id="ex-note">Aggiungi nota</button>${stato.modo !== "demo" && stato.modificato ? `<button id="ex-save">${stato.modo === "riserva" ? "Scarica JSON" : "Salva archivio"}</button>` : ""}<button data-vista="executive">Esci dal meeting</button>`
          : `<label>Dal <input type="date" id="settimanale-dal" max="${giorno}" value="${esc2(stato.dal)}"></label>${pulsanteAvvisiAttivita(avvisi)}<button data-vista="meeting">Presenta allo Staff →</button>`}</div>
      </header>
      ${meeting ? `<nav class="pmo-deck" aria-label="Pagine del meeting">${sezioniExecutive.map((titolo, indice) => `<button data-ex-page="${indice}" aria-current="${indice === paginaExecutive ? "step" : "false"}">${titolo}</button>`).join("")}</nav>` : ""}
      ${meeting ? contenuti[paginaExecutive] : contenuti.join("")}
      ${meeting ? `<footer class="pmo-deck"><button data-ex-page="${paginaExecutive - 1}" ${paginaExecutive === 0 ? "disabled" : ""}>← Indietro</button><span>${paginaExecutive + 1} / ${sezioniExecutive.length}</span><button data-ex-page="${paginaExecutive + 1}" ${paginaExecutive === sezioniExecutive.length - 1 ? "disabled" : ""}>Avanti →</button></footer>` : ""}
    </div>`;
  }
  function htmlGuidaExecutive() {
    return `<div class="pmo">
      <header class="pmo-hero"><div><p class="pmo-eyebrow">COLLETTORE INGEGNERIA</p><h1>Guida all’uso</h1></div>
        <div class="pmo-controls"><button data-vista="executive">Vai a Executive →</button></div>
      </header>
      <div class="pmo-grid pmo-guida">
        <section class="pmo-panel"><h2>Apri e aggiorna l’archivio</h2><p>All’avvio apri la cartella condivisa, carica il JSON se il browser lo prevede oppure prova i dati demo. Seleziona il tuo nome nella barra Utente prima di modificare una scheda.</p><p>In Executive scegli la data di inizio del periodo. La data filtra aggiornamenti e decisioni. KPI, warning e roadmap mostrano la situazione corrente. Seleziona un KPI per vedere le voci e usa il loro codice per aprire la scheda.</p></section>
        <section class="pmo-panel"><h2>Segnala un warning</h2><p>Per segnalare un’attività, modifica la scheda e compila <strong>Warning</strong>. L’avviso appare in Area Team ed Executive quando l’attività è visibile in quelle viste. Puoi riaprirlo dal pulsante <strong>Warning attività</strong>. Per rimuoverlo, svuota il campo o chiudi l’attività.</p><p>Per segnalare un rischio di tema, usa <strong>Aggiungi warning</strong>. I rischi a impatto Alto appaiono nell’elenco dei warning. Salva l’archivio per conservare le modifiche.</p></section>
        <section class="pmo-panel"><h2>Conduci lo Staff meeting</h2><p>Da Executive scegli <strong>Presenta allo Staff</strong>, poi <strong>Schermo intero</strong>. Usa i pulsanti o le frecce ← e → per cambiare vista. In <strong>Per stream</strong> espandi uno stream per vedere attività aperte e note collegate.</p><p>Usa <strong>Aggiungi nota</strong> da qualsiasi vista dello Staff e collegala a una scheda. La ritrovi in <strong>Note</strong> e negli aggiornamenti dell’archivio. Quando hai finito, salva l’archivio o scarica il JSON.</p></section>
        <section class="pmo-panel"><h2>Salva e condividi</h2><p>Usa <strong>Salva</strong> per aggiornare l’archivio collegato o <strong>Scarica collettore.json</strong> per conservare il file aggiornato. <strong>Esporta Excel</strong> scarica una copia per consultazione. Le modifiche ai dati demo non vengono salvate.</p></section>
        <section class="pmo-panel"><h2>Consulta il dettaglio di un team</h2><p>Apri <strong>Area Team</strong>, scegli uno stream e al primo accesso imposta una password di almeno 10 caratteri diversa da quelle degli altri team. L’area si blocca quando cambi team, esci dalla vista o scegli <strong>Blocca area</strong>.</p><p>Le password sono locali al browser. L’accesso all’archivio dipende dai permessi della cartella condivisa.</p></section>
        <section class="pmo-panel"><h2>Attività tra più team</h2><p>In Area Team, scegli <strong>Nuova attività</strong>. Il team corrente è preselezionato. Puoi aggiungere altri team coinvolti. L’attività resta fuori da Executive e Staff finché un team non sceglie di condividerla. Una volta condivisa, conta una sola volta nei KPI complessivi.</p></section>
        <section class="pmo-panel"><h2>Condividi una voce con il PMO</h2><p>Dalla zona del team, scegli <strong>Condividi con PMO</strong> su una voce. Comparirà in <strong>Condiviso dai team</strong>, su Executive e nello Staff, finché almeno un team la condivide. Salva l’archivio dopo ogni modifica. Le altre viste PMO continuano a seguire i loro filtri.</p></section>
      </div>
    </div>`;
  }
  function paginaMeeting(indice) {
    if (indice < 0 || indice >= sezioniExecutive.length) return;
    paginaExecutive = indice;
    mostraVista();
    window.scrollTo(0, 0);
    document.querySelector('.pmo-deck [aria-current="step"]')?.focus({ preventScroll: true });
  }
  function collegaExecutive() {
    $("#vista").addEventListener("submit", accediTeam);
    $("#vista").addEventListener("click", evento => {
      const pulsante = evento.target.closest("button");
      if (!pulsante) return;
      if (pulsante.id === "ex-alerts") {
        mostraAvvisiAttivita(avvisiVistaAttuale());
      } else if (pulsante.id === "ex-team-new" && stato.vista === "team" && teamSbloccati) {
        const stream = stato.ds.elenchi.stream[teamSelezionato];
        apriForm("ATT", { stream }, { rifissa: { soloAreaTeam: true } });
      } else if (pulsante.dataset.exPmo !== undefined) {
        aggiornaCondivisionePMO(pulsante.dataset.exPmo);
      } else if (pulsante.id === "ex-team-lock") {
        teamSbloccati = false;
        mostraVista();
      } else if (pulsante.dataset.exTeam !== undefined) {
        const indice = Number(pulsante.dataset.exTeam);
        if (indice === teamSelezionato) return;
        teamSbloccati = false;
        teamSelezionato = indice;
        mostraVista();
        document.querySelector('[data-ex-team][aria-current="page"]')?.focus();
      } else if (pulsante.dataset.exFocus) {
        const chiave = pulsante.dataset.exFocus;
        focusExecutive = focusExecutive === chiave ? "" : chiave;
        mostraVista();
        document.querySelector(`[data-ex-focus="${chiave}"]`)?.focus();
      } else if (pulsante.dataset.exPage !== undefined) {
        paginaMeeting(Number(pulsante.dataset.exPage));
      } else if (pulsante.id === "ex-note") {
        apriNotaStaff();
      } else if (pulsante.id === "ex-save") {
        salva();
      } else if (pulsante.id === "ex-full") {
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        else if (document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen().catch(() => messaggio("Schermo intero non disponibile.", "avviso"));
        } else messaggio("Schermo intero non disponibile.", "avviso");
      }
    });
    document.addEventListener("keydown", evento => {
      if (stato.vista !== "meeting" || $("#finestra").open || evento.altKey || evento.ctrlKey || evento.metaKey) return;
      if (evento.key === "ArrowRight" || evento.key === "ArrowLeft") {
        evento.preventDefault();
        paginaMeeting(paginaExecutive + (evento.key === "ArrowRight" ? 1 : -1));
      }
    });
  }
