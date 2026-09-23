#!/usr/bin/env node
// protected-manifest.mjs — CONTROLE MECANICO (nao heuristico) para pastas protegidas (Constitution §6, §19; THREAT-MODEL.md).
// O guard (hooks/guard-protected-paths.mjs) tenta impedir escrita por comando e e HEURISTICO. Este script prova, por hash, que o conteudo das
// pastas protegidas NAO mudou, qualquer que tenha sido o caminho (comando ofuscado, script em arquivo, ferramenta fora do matcher, edicao manual).
// Requer so Node >= 18 (sem dependencias). Nunca imprime conteudo de arquivo (nem hashes nas divergencias): so o caminho e o tipo da divergencia.
//
// Uso:
//   node protected-manifest.mjs --init  [--dirs dados,validador] [--root <raiz>] [--manifest <arquivo>] [--force]
//   node protected-manifest.mjs --check [--dirs ...]              [--root <raiz>] [--manifest <arquivo>] [--ignore-eol]
//   --dirs      lista separada por virgula, RELATIVA a raiz (padrao: env PROTECTED_DIRS). No --check o padrao e a lista gravada no manifesto.
//   --root      raiz do projeto (padrao: env CLAUDE_PROJECT_DIR, senao o cwd).
//   --manifest  arquivo do manifesto (padrao: <raiz>/PROTECTED-MANIFEST.json). Deve ficar FORA das pastas protegidas e ser versionado/revisado.
//   --force     permite --init sobrescrever um manifesto existente (re-baseline consciente; a mudanca aparece no diff do commit).
//   --ignore-eol (so --check; NUNCA e o padrao) tolera arquivo cujo conteudo so difere do manifesto por CRLF<->LF (compara tambem o arquivo normalizado para LF e para CRLF).
//               RISCO: uma alteracao que mude APENAS o fim de linha (ex.: CRLF -> LF num script, ou dado tabular) passa despercebida; e a conferencia so vale para arquivos de texto
//               com fim de linha uniforme (com fim de linha misto no original a tolerancia pode nao se aplicar). Prefira `.gitattributes` com `<pasta>/** -text` e regerar o manifesto; use a flag so como decisao consciente.
// Saida: exit 0 = conferido; exit 2 = divergencia (lista ALTERADO/NOVO/REMOVIDO/PASTA AUSENTE) ou erro de leitura/arquivo de manifesto invalido;
//   exit 1 = uso incorreto (argumentos). O manifesto e deterministico (sem data; chaves ordenadas): o mesmo conteudo gera os mesmos bytes.
// O que cobre: arquivo alterado (SHA-256 dos BYTES, inclusive fim de linha), novo, removido, renomeado (= removido + novo), pasta protegida ausente,
//   symlink (o alvo do link entra como conteudo; nao e seguido). Ignora qualquer pasta OU arquivo chamado `.git` (em qualquer profundidade) — inclusive um `.git` DENTRO da
//   pasta protegida: alterar/criar conteudo em `<pasta>/.git/...` NAO e visto (limite declarado no THREAT-MODEL §5).
//   Divergencia so de fim de linha e rotulada `[so fim de linha]` com a causa provavel (autocrlf/.gitattributes): `git checkout` NAO a resolve.
// r9: --check recusa (exit 2) manifesto com `dirs` vazio, ou sem nenhum arquivo em lugar nenhum: um controle que nao verifica nada nao reporta sucesso.
// LIMITES: pasta vazia nao e registrada (o git tambem nao a versiona); mudanca de permissao/data nao e detectada (so conteudo e nome); fluxos alternativos de dados do NTFS
//   (arquivo:fluxo) NAO sao lidos (o readFileSync le so o fluxo principal: escrever num fluxo oculto de arquivo protegido passa);
//   se o manifesto puder ser reescrito pelo mesmo agente que altera os dados, a prova some: versione-o e confira no CI (templates/ci/protected-paths.yml)
//   contra a versao aprovada; conversao automatica de fim de linha no checkout (autocrlf) muda os bytes: use `.gitattributes` (`<dir>/** -text`).
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readdirSync, readFileSync, readlinkSync, realpathSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const DEFAULT_MANIFEST = 'PROTECTED-MANIFEST.json';
const die = (code, msg) => { console.error(msg); process.exit(code); };

function parseArgs(argv) {
  const a = { mode: null, dirs: null, root: null, manifest: null, force: false, ignoreEol: false };
  for (let i = 0; i < argv.length; i++) {
    const t = argv[i];
    const val = () => { const v = argv[++i]; if (v === undefined || v.startsWith('--')) die(1, `protected-manifest: ${t} exige um valor`); return v; };
    if (t === '--init' || t === '--check') { if (a.mode && a.mode !== t) die(1, 'protected-manifest: use --init OU --check, nao os dois'); a.mode = t; }
    else if (t === '--dirs') a.dirs = val();
    else if (t === '--root') a.root = val();
    else if (t === '--manifest') a.manifest = val();
    else if (t === '--force') a.force = true;
    else if (t === '--ignore-eol') a.ignoreEol = true;
    else die(1, `protected-manifest: argumento desconhecido "${t}". Uso: --init|--check [--dirs a,b] [--root dir] [--manifest arq] [--force] [--ignore-eol]`);
  }
  if (a.ignoreEol && a.mode === '--init') die(1, 'protected-manifest: --ignore-eol so vale com --check (o manifesto e sempre gravado com os bytes exatos)');
  if (!a.mode) die(1, 'protected-manifest: informe --init ou --check. Uso: node protected-manifest.mjs --init|--check [--dirs a,b] [--root dir] [--manifest arq] [--force]');
  return a;
}

// code = exit code das recusas: 1 (uso) para o que o USUARIO digitou; 2 (manifesto invalido) para o que veio do ARQUIVO do manifesto adulterado
function normDirs(list, code = 1) {
  const out = [];
  for (const raw of list) {
    let d = String(raw).trim().replace(/\\/g, '/').replace(/^(\.\/)+/, '').replace(/\/+$/, '');
    if (!d) continue;
    if (path.isAbsolute(d) || /^[A-Za-z]:/.test(d)) die(code, `protected-manifest: pasta protegida deve ser RELATIVA a raiz (recebido: "${raw}")`);
    if (d.split('/').some((s) => s === '..' || s === '.')) die(code, `protected-manifest: pasta protegida nao pode conter "." ou ".." (recebido: "${raw}")`);
    if (d.split('/').includes('.git')) die(code, `protected-manifest: .git nao pode ser pasta protegida (recebido: "${raw}")`);
    if (!out.includes(d)) out.push(d);
  }
  return out.sort();
}

// O arquivo `target` cai DENTRO da pasta `dirAbs`? A comparacao NAO pode ser so textual: process.cwd() devolve o caminho REAL (no macOS o temp
// /var/... e symlink de /private/var/...; junction/symlink de pasta em qualquer SO), enquanto --manifest pode vir pelo caminho LOGICO; e o macOS/Windows
// ignoram maiusculas/minusculas. Por isso a identidade e por (dev, ino) da pasta e de cada ancestral EXISTENTE do manifesto (o SO resolve symlinks e "..");
// o realpath do manifesto (se ele ja existir, inclusive como symlink para dentro da pasta) e a comparacao textual antiga ficam como redundancia.
// So prende demais (recusa) quando ha duvida real de identidade; nunca deixa passar um caminho que o SO resolve para dentro da pasta.
function insideDir(dirAbs, targetAbs, targetRaw) {
  const idOf = (p) => { try { const s = statSync(p, { bigint: true }); return `${s.dev}:${s.ino}`; } catch { return null; } };
  const dirId = idOf(dirAbs);
  const textual = (a, b) => { const rel = path.relative(a, b); return !!rel && !rel.startsWith('..') && !path.isAbsolute(rel); };
  if (textual(dirAbs, targetAbs)) return true;
  if (dirId) {
    // ancestrais do manifesto pelo caminho como o usuario o digitou (o SO resolve symlink antes de "..") e pelo normalizado
    for (const start of new Set([path.dirname(path.resolve(String(targetRaw))), path.dirname(targetAbs)])) {
      let cur = start;
      for (;;) {
        if (idOf(cur) === dirId) return true;
        const up = path.dirname(cur); if (up === cur) break; cur = up;
      }
    }
  }
  try { // manifesto ja existente que e symlink (ou esta atras de um): o alvo REAL cai dentro da pasta?
    const realTarget = realpathSync.native(targetAbs); const realDir = realpathSync.native(dirAbs);
    if (textual(realDir, realTarget)) return true;
  } catch { /* manifesto ainda nao existe: ja coberto pelos ancestrais acima */ }
  return false;
}

function sha256(buf) { return createHash('sha256').update(buf).digest('hex'); }

// Percorre a pasta e devolve { "<dir>/<sub>/<arq>": hash } — ignora `.git`; symlink nao e seguido (entra como "symlink:<alvo>").
function walk(rootAbs, relDir, into, errors) {
  let entries;
  try { entries = readdirSync(path.join(rootAbs, relDir), { withFileTypes: true }); }
  catch { errors.push(`${relDir}: nao foi possivel listar`); return; }
  for (const e of entries) {
    if (e.name === '.git') continue;
    const rel = `${relDir}/${e.name}`;
    const abs = path.join(rootAbs, relDir, e.name);
    try {
      const st = lstatSync(abs);
      if (st.isSymbolicLink()) into[rel] = sha256(Buffer.from(`symlink:${readlinkSync(abs)}`));
      else if (st.isDirectory()) walk(rootAbs, rel, into, errors);
      else if (st.isFile()) into[rel] = sha256(readFileSync(abs));
      else into[rel] = sha256(Buffer.from('special-file'));
    } catch { errors.push(`${rel}: nao foi possivel ler`); }
  }
}

function snapshot(rootAbs, dirs) {
  const files = {}; const errors = []; const missing = [];
  for (const d of dirs) {
    const abs = path.join(rootAbs, d);
    if (!existsSync(abs) || !lstatSync(abs).isDirectory()) { missing.push(d); continue; }
    walk(rootAbs, d, files, errors);
  }
  const sorted = Object.fromEntries(Object.keys(files).sort().map((k) => [k, files[k]]));
  return { files: sorted, errors, missing };
}

// so fim de linha: o arquivo atual normalizado para LF ou para CRLF (byte a byte, sem decodificar) reproduz o hash do manifesto?
const eolHashes = (buf) => { const lf = Buffer.from(buf.toString('latin1').replace(/\r\n/g, '\n'), 'latin1'); const crlf = Buffer.from(lf.toString('latin1').replace(/\n/g, '\r\n'), 'latin1'); return [sha256(lf), sha256(crlf)]; };
function eolOnly(rootAbs, rel, expected) {
  try { const abs = path.join(rootAbs, ...rel.split('/')); if (!lstatSync(abs).isFile()) return false; return eolHashes(readFileSync(abs)).includes(expected); } catch { return false; }
}

function treeHash(files) {
  return sha256(Buffer.from(Object.entries(files).map(([k, v]) => `${v}  ${k}\n`).join('')));
}

const args = parseArgs(process.argv.slice(2));
const rootAbs = path.resolve(args.root ?? process.env.CLAUDE_PROJECT_DIR ?? process.cwd());
const manifestPath = path.resolve(args.manifest ?? path.join(rootAbs, DEFAULT_MANIFEST));
const fromEnv = (process.env.PROTECTED_DIRS ?? '').split(',');

if (args.mode === '--init') {
  const dirs = normDirs(args.dirs ? args.dirs.split(',') : fromEnv);
  if (!dirs.length) die(1, 'protected-manifest: nenhuma pasta protegida (use --dirs a,b ou a env PROTECTED_DIRS)');
  if (existsSync(manifestPath) && !args.force) die(2, `protected-manifest: ja existe um manifesto em ${manifestPath}. Recusado para nao mudar a linha de base em silencio; use --force se o re-baseline for consciente (e revise o diff do commit).`);
  const { files, errors, missing } = snapshot(rootAbs, dirs);
  if (missing.length) die(2, `protected-manifest: pasta(s) protegida(s) inexistente(s) na raiz ${rootAbs}: ${missing.join(', ')}. Manifesto NAO gravado.`);
  if (errors.length) die(2, `protected-manifest: erro de leitura, manifesto NAO gravado:\n  ${errors.join('\n  ')}`);
  for (const d of dirs) { // manifesto dentro da pasta protegida se invalidaria a si mesmo
    if (insideDir(path.join(rootAbs, d), manifestPath, args.manifest ?? manifestPath)) die(2, `protected-manifest: o manifesto nao pode ficar dentro da pasta protegida "${d}".`);
  }
  const doc = { version: 1, algorithm: 'sha256', dirs, tree_sha256: treeHash(files), files };
  try { writeFileSync(manifestPath, `${JSON.stringify(doc, null, 2)}\n`); } catch (e) { die(2, `protected-manifest: nao consegui gravar o manifesto em ${manifestPath} (${e.code ?? 'erro'}: a pasta de destino existe e e gravavel?). Manifesto NAO gravado.`); }
  console.log(`protected-manifest: manifesto gravado em ${manifestPath} — ${Object.keys(files).length} arquivo(s) em ${dirs.length} pasta(s) [${dirs.join(', ')}]; tree_sha256=${doc.tree_sha256.slice(0, 16)}...`);
  console.log('protected-manifest: versione o manifesto e revise qualquer mudanca dele como mudanca de linha de base.');
  process.exit(0);
}

// --check
if (!existsSync(manifestPath)) die(2, `protected-manifest: manifesto nao encontrado em ${manifestPath} (rode --init antes, e versione o arquivo).`);
let doc;
try { doc = JSON.parse(readFileSync(manifestPath, 'utf8')); } catch { die(2, `protected-manifest: manifesto ilegivel ou nao e JSON: ${manifestPath}`); }
if (!doc || doc.version !== 1 || doc.algorithm !== 'sha256' || !Array.isArray(doc.dirs) || typeof doc.files !== 'object' || doc.files === null || Array.isArray(doc.files)) die(2, 'protected-manifest: manifesto com formato invalido (esperado version 1, algorithm sha256, dirs[], files{}).');
if (typeof doc.tree_sha256 !== 'string' || doc.tree_sha256 !== treeHash(doc.files)) die(2, 'protected-manifest: manifesto INCONSISTENTE (tree_sha256 nao confere com files{}): editado a mao? Regere com --init --force num commit revisado.');
const dirs = normDirs(doc.dirs, 2); // dirs vindas do manifesto: recusa = manifesto invalido (exit 2), nao erro de uso
// r9 (L4): um --check que nao verifica nada NAO pode reportar sucesso (manifesto com `dirs: []`, ou sem nenhum arquivo e sem nenhum arquivo agora)
if (!dirs.length) die(2, 'protected-manifest: manifesto sem NENHUMA pasta protegida (dirs vazio): nada a conferir; recusado em vez de "CONFERIDO". Regere com --init --dirs <pastas> num commit revisado.');
if (args.dirs || process.env.PROTECTED_DIRS) {
  const asked = normDirs(args.dirs ? args.dirs.split(',') : fromEnv);
  if (asked.length && asked.join('|') !== dirs.join('|')) die(2, `protected-manifest: a lista de pastas pedida [${asked.join(', ')}] difere da do manifesto [${dirs.join(', ')}]. Manifesto desatualizado ou pasta protegida nova sem baseline.`);
}
const now = snapshot(rootAbs, dirs);
const problems = []; const tolerated = []; let eolCount = 0;
if (!Object.keys(doc.files).length && !Object.keys(now.files).length && !now.missing.length && !now.errors.length) die(2, `protected-manifest: nenhum arquivo registrado no manifesto e nenhum arquivo nas pastas [${dirs.join(', ')}]: nada a conferir; recusado em vez de "CONFERIDO" (proteja uma pasta com conteudo ou regere o manifesto).`);
for (const d of now.missing) problems.push(`PASTA AUSENTE  ${d}`);
for (const m of now.errors) problems.push(`ERRO DE LEITURA  ${m}`);
for (const k of Object.keys(doc.files).sort()) {
  if (!(k in now.files)) { if (!now.missing.some((d) => k === d || k.startsWith(`${d}/`))) problems.push(`REMOVIDO       ${k}`); }
  else if (now.files[k] !== doc.files[k]) {
    const eo = eolOnly(rootAbs, k, doc.files[k]);
    if (eo && args.ignoreEol) tolerated.push(k);
    else { problems.push(`ALTERADO       ${k}${eo ? '  [so fim de linha CRLF<->LF]' : ''}`); if (eo) eolCount++; }
  }
}
for (const k of Object.keys(now.files).sort()) if (!(k in doc.files)) problems.push(`NOVO           ${k}`);
if (problems.length) {
  console.error(`protected-manifest: DIVERGENCIA em ${problems.length} item(ns) das pastas protegidas [${dirs.join(', ')}] (conteudo nunca impresso):`);
  for (const p of problems) console.error(`  ${p}`);
  console.error(`protected-manifest: se a mudanca foi autorizada, gere o manifesto novo (--init --force) num commit revisado por humano; se nao, reverta (git checkout -- <pasta>).`);
  if (eolCount) console.error(`protected-manifest: ${eolCount} divergencia(s) SO de fim de linha (CRLF<->LF): causa provavel = core.autocrlf / falta de .gitattributes (manifesto gerado e conferido com conversoes diferentes). \`git checkout -- <pasta>\` NAO resolve isso. Correcao: versione .gitattributes com \`<pasta>/** -text\` (bytes identicos em toda plataforma) e regere o manifesto (--init --force) num commit revisado; so como decisao consciente use --ignore-eol (tolera fim de linha; ver o risco no cabecalho).`);
  process.exit(2);
}
if (tolerated.length) console.log(`protected-manifest: AVISO --ignore-eol: ${tolerated.length} arquivo(s) conferido(s) SO apos normalizar fim de linha (CRLF<->LF). Neste modo uma alteracao que mude apenas o fim de linha NAO e detectada.`);
console.log(`protected-manifest: CONFERIDO — ${Object.keys(now.files).length} arquivo(s) em ${dirs.length} pasta(s) [${dirs.join(', ')}]; tree_sha256=${treeHash(now.files).slice(0, 16)}...`);
process.exit(0);
