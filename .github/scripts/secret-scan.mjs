#!/usr/bin/env node
// PreToolUse (Bash/PowerShell com `git ... commit`) e uso manual: bloqueia (exit 2) se o que ENTRARIA no commit tiver .env ou padrao de segredo.
// Constitution §10 (segredo suspeito => parar; nunca registrar o valor) e §5 (commit e mutacao). Suite de referencia: tests/secret-scan.test.mjs.
// Requer Node (>=18) e git; suite exercitada no Windows (local) e nas 3 plataformas pelo CI do docs2 (Linux/macOS do runner; o harness real so foi medido no Windows/PowerShell). NUNCA imprime o valor: so arquivo:linha e tipo do padrao.
//
// ATENCAO — ESTE HOOK DE COMANDO E HEURISTICO. O controle MECANICO real e o hook do git `pre-commit` (templates/hooks/git-pre-commit), que roda
//   `node secret-scan.mjs --staged` NO MOMENTO do commit, seja qual for o caminho que o originou (git ci, alias, bash -c, cmd /c, eval, script,
//   IDE...). Instale os DOIS (ver hooks/README.md). Este hook do harness ajuda a barrar CEDO e com mensagem clara, mas nao e a barreira.
//
// O que e varrido:
//   - modo padrao (hook ou `--staged`): o conteudo do INDICE, nao o working tree; lido em LOTE (`git diff --cached --raw` + `git cat-file --batch`, ~4 ms/arquivo;
//     antes era um `git show :<arquivo>` por arquivo, ~60 ms cada).
//   - se o commit usa -a/--all (detectado so nas OPCOES do `git commit`, nao no texto da mensagem): tambem os arquivos rastreados modificados, lidos do working tree;
//     se o commit lista caminhos (`git commit <caminho>`): esses caminhos, lidos do working tree.
//   - se o MESMO comando tem `git add ... ` antes do commit (`git add -A && git commit`, `git add -f .env && git commit`): os arquivos que o add incluiria
//     (nomeados, ou todo o working tree para -A/./-u; com -f tambem os ignorados), lidos do working tree.
//   - a MENSAGEM do commit (-m/--message/-F arquivo) tambem e varrida.
//   - `--all` (uso manual): todos os arquivos rastreados, lidos do working tree.
//   Nomes vem com `-z` (acentos/espacos ok). Roda a partir de `git rev-parse --show-toplevel` (funciona em subdiretorio).
// Reconhece `git commit` em POSICAO DE COMANDO (`git`, `git -c k="a b" commit`, `git -C dir commit`, `git.exe commit`, apos && ; | sudo env VAR=x ...),
//   dentro de `bash -c "..."`, `sh -c`, `cmd /c`, `pwsh -c`, `eval "..."`/`iex`, e por ALIAS configurado no git (alias.<x> = commit ...); `cd dir &&` e seguido.
//   `echo "git commit"` ou texto dentro de aspas (fora dos casos acima) NAO aciona o hook.
// Arquivos UTF-16 (BOM ou padrao de NULs) sao DECODIFICADOS e varridos, com AVISO; binarios sao varridos como texto latino (segredo em formato binario,
//   ex. .pfx, nao e detectavel) com AVISO — nunca pulados em silencio. Nomes de credencial (id_rsa, *.pfx, *.p12, *.jks, *.keystore) bloqueiam por nome.
// FALHA FECHADA (exit 2 com aviso): nao conseguir ler um arquivo do commit (exceto deletado ou submodulo) ou nao conseguir listar os arquivos.
//   Fail-open (exit 0, com aviso) so quando a entrada JSON e invalida ou nao ha repositorio git.
//
// Configuracao (env):
//   EXTRA_SECRET_PATTERNS = lista JSON de regex, ex.: '["MEU_SEGREDO\\s*=\\s*\\S{16,}"]'. Cada regex e validada separadamente; uma invalida e ignorada com aviso
//     que cita so o INDICE (#N), nunca o texto (ele pode conter o valor). Os hits mostram `padrao extra #N`.
//   SECRET_ALLOW_FILES    = lista JSON de regex de caminhos isentos (ex.: '["^docs/evidencias/"]'), casada com o caminho relativo em barras `/`.
// Padroes embutidos: atribuicao de segredo com valor literal (case-insensitive; aceita chave entre aspas como em JSON, valor com simbolos, e valor com espacos entre
//   aspas quando a chave e exatamente password/senha/secret/token/apikey), hex de 64 chars (SO com palavra-chave na linha ou no nome do arquivo: hashes de evidencia
//   SHA-256 nao bloqueiam), chave privada PEM, token GitHub (ghp_/github_pat_), chave sk-/sk-ant-/sk_live_, AWS AKIA, Google AIza, Slack xox*, JWT,
//   AccountKey= (Azure), URL com credencial, flag de CLI com valor literal (--token <valor>, --password=<valor>), chaves com hifen (x-api-key:) e DB_PASS=/MYSQL_PWD=.
//   `.env` bloqueia; `.env.example|.sample|.template|.dist` nao. Senha com parenteses ou iniciada por $ NAO e detectada (parenteses geram falso positivo em codigo).
// LIMITES: 2a camada (a 1a e o .gitignore); nao ve historico ja publicado (rode `git log -p --all`) nem substitui ferramenta dedicada (ex.: gitleaks);
//   heuristica de valor literal gera falso positivo (ex.: atribuir a uma variavel de nome "token" o nome de uma funcao longa; textos i18n de tela de login); segredos fora dos formatos
//   acima passam (valor em linha separada de YAML, `pwd:`, formatos de outros provedores); `git commit-tree`, `merge`, `cherry-pick` e alias com `-c alias.x=` na linha nao sao vistos.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const warn = (m) => console.error(`secret-scan: AVISO: ${m}`);
const KEYWORD = /(secret|key|token|password|senha|chave|credential)/i;
const BUILTIN_ALLOW = /(^|\/)(package-lock\.json|SHA256SUMS\.txt)$/; // hashes legitimos (nao sao segredo)
const ENV_FILE = /(^|\/)\.env$|(^|\/)\.env\.(?!(?:example|sample|template|dist)$)/;
const CRED_FILE = /(^|\/)(id_rsa|id_ed25519|id_ecdsa|id_dsa)$|\.(pfx|p12|jks|keystore)$/i;

function jsonList(envName) {
  const raw = process.env[envName];
  if (!raw) return [];
  let v;
  try { v = JSON.parse(raw); } catch { warn(`${envName} nao e JSON valido (ignorada)`); return []; }
  if (!Array.isArray(v)) { warn(`${envName} deve ser uma lista JSON (ignorada)`); return []; }
  return v;
}
function compileList(envName, flags = '') {
  const out = [];
  jsonList(envName).forEach((s, i) => {
    if (typeof s !== 'string') { warn(`${envName} #${i + 1} nao e string (ignorada)`); return; }
    try { out.push([i + 1, new RegExp(s, flags)]); } catch { warn(`${envName} #${i + 1} nao e regex valida (ignorada)`); }
  });
  return out;
}

// chaves reconhecidas em atribuicoes (nome pode ter sufixo: DB_PASSWORD, API_TOKEN_2, ...)
const KEYS = 'SECRET|PASSWORD|PASSWD|SENHA|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|(?:APP|JWT|SIGNING|ENCRYPTION|AUTH|SESSION|MASTER)[_-]?KEY';
const NOT_REF = '(?![$<{%])'; // valor que comeca por $VAR, <placeholder>, {{template}} ou %VAR% nao e literal
const PATTERNS = [
  ['atribuicao de segredo com valor literal', new RegExp(`(?:${KEYS})[A-Za-z0-9_-]*["']?\\s*[=:]\\s*["']?${NOT_REF}[A-Za-z0-9+/=_@!#$%^&*.~:?-]{16,}`, 'i')],
  ['atribuicao de segredo com valor entre aspas contendo espacos', new RegExp(`(?<![A-Za-z])(?:PASSWORD|PASSWD|PASS|SENHA|SECRET|TOKEN|API_?KEY)["']?\\s*[=:]\\s*(["'])${NOT_REF}(?=[^"'\\r\\n]* )[^"'\\r\\n]{16,}\\1`, 'i')],
  ['atribuicao pass/pwd (chave exata) com valor literal sem barras', new RegExp(`(?<![A-Za-z0-9])(?:PASS|PWD)["']?\\s*[:=]\\s*["']?${NOT_REF}[A-Za-z0-9+=_@!#%^&*.?-]{16,}`, 'i')],
  ['flag de CLI com segredo literal (--token <valor>, --password=<valor>)', new RegExp(`(?<![A-Za-z0-9-])--(?:token|password|passwd|secret|api-?key|access-?key|client-?secret)(?:=|[ \t]+)["']?${NOT_REF}[A-Za-z0-9+/=_@!#$%^&*.~:?-]{16,}`, 'i')],
  ['hex de 64 chars com palavra-chave (possivel chave de 32 bytes)', /\b[0-9a-fA-F]{64}\b/, true],
  ['chave privada PEM', /-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----/i],
  ['token GitHub', /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b/],
  ['chave de provedor de LLM', /\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b/],
  ['chave live de pagamento (Stripe)', /\b[sr]k_live_[A-Za-z0-9]{16,}\b/],
  ['AWS access key', /\bAKIA[0-9A-Z]{16}\b/],
  ['Google API key', /\bAIza[0-9A-Za-z_-]{35}\b/],
  ['token Slack', /\bxox[baprs]-[A-Za-z0-9-]{10,}/],
  ['JWT', /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/],
  ['AccountKey (Azure)', /\bAccountKey=[A-Za-z0-9+/]{40,}={0,2}/],
  ['URL com credencial embutida', /\b[a-z][a-z0-9+.-]*:\/\/[^\s/:@'"]+:[^\s/@'"]{6,}@[^\s/'"]+/i],
];

// ---------- reconhecimento de `git commit` (e `git add`) em posicao de comando ----------
function stagesOf(cmd, isPS) {
  const out = []; let toks = []; let tok = ''; let has = false; let q = null; const heredocs = [];
  const n = cmd.length;
  const flush = () => { if (has) toks.push(tok); tok = ''; has = false; };
  const end = () => { flush(); if (toks.length) out.push(toks); toks = []; };
  const skipWord = (i) => { let j = i; let qq = null; for (; j < n; j++) { const c = cmd[j]; if (qq) { if (c === qq) qq = null; continue; } if (c === '"' || c === "'") { qq = c; continue; } if (/[\s|;&<>]/.test(c)) break; } return j; };
  for (let i = 0; i < n; i++) {
    const c = cmd[i];
    if (q) {
      if (q === '"' && ((isPS && c === '`') || (!isPS && c === '\\')) && i + 1 < n) { tok += cmd[i + 1]; i++; continue; }
      if (c === q) { if (isPS && cmd[i + 1] === q) { tok += q; i++; continue; } q = null; continue; }
      tok += c; continue;
    }
    if (c === '"' || c === "'") { q = c; has = true; continue; }
    if (c === '`' && isPS && i + 1 < n) { tok += cmd[i + 1]; has = true; i++; continue; }
    if (c === ' ' || c === '\t' || c === '\r') { flush(); continue; }
    if (c === '\n') {
      end();
      while (heredocs.length) {
        const w = heredocs.shift(); let j = i + 1;
        while (j <= n) { const nl = cmd.indexOf('\n', j); const line = cmd.slice(j, nl === -1 ? n : nl).replace(/\r$/, ''); j = nl === -1 ? n + 1 : nl + 1; if (line.trim() === w) break; }
        i = Math.min(j - 1, n);
      }
      continue;
    }
    if (c === ';') { end(); continue; }
    if (c === '|') { end(); if (cmd[i + 1] === '|' || cmd[i + 1] === '&') i++; continue; }
    if (c === '&') { if (cmd[i + 1] === '>') continue; end(); if (cmd[i + 1] === '&') i++; continue; }
    if (c === '>') { flush(); let j = i + 1; if (cmd[j] === '>') j++; if (cmd[j] === '&') j++; while (cmd[j] === ' ') j++; i = skipWord(j) - 1; continue; }
    if (c === '<') {
      if (cmd[i + 1] === '<' && cmd[i + 2] !== '<') { let j = i + 2; if (cmd[j] === '-') j++; while (cmd[j] === ' ') j++; const e = skipWord(j); const w = cmd.slice(j, e).replace(/^['"]|['"]$/g, ''); flush(); if (w) heredocs.push(w); i = e - 1; continue; }
      flush(); i = skipWord(cmd[i + 1] === '<' ? i + 3 : i + 1) - 1; continue;
    }
    tok += c; has = true;
  }
  end();
  return out;
}
const baseName = (t) => String(t).toLowerCase().split(/[\\/]/).pop().replace(/\.(exe|cmd|bat)$/, '');
const GIT_OPT_VALUE = new Set(['-c', '-C', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env']);
const COMMIT_SHORT_VALUE = new Set(['m', 'F', 'C', 'c', 't']);
const COMMIT_LONG_VALUE = new Set(['message', 'file', 'author', 'date', 'reuse-message', 'reedit-message', 'fixup', 'squash', 'template', 'cleanup', 'trailer', 'pathspec-from-file']);
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'ksh', 'cmd', 'pwsh', 'powershell']);
const EVALS = new Set(['eval', 'invoke-expression', 'iex']);
const CD_VERBS = new Set(['cd', 'chdir', 'set-location', 'sl', 'pushd', 'push-location']);

// analisa o comando e devolve { found, all, paths, cdirs, adds, messages, msgFiles }.
// aliasOf(nome) -> texto do alias configurado no git ou null (pode ser omitido).
function findGitCommit(cmd, isPS, aliasOf = () => null, depth = 0, acc = null, chain = []) {
  acc ??= { found: false, all: false, paths: [], cdirs: [], adds: [], messages: [], msgFiles: [] };
  chain = chain.slice();
  for (const raw of stagesOf(cmd, isPS)) {
    let tk = raw.slice();
    for (let g = 0; g < 20 && tk.length; g++) {
      const t0 = tk[0].replace(/^[$({!&]+/, '');
      if (t0 === '') { tk.shift(); continue; }
      const l = baseName(t0);
      if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t0)) { tk.shift(); continue; }
      if (l === 'sudo' || l === 'doas') { tk.shift(); while (tk.length && /^-/.test(tk[0])) { const f = tk.shift(); if (/^-[ughpCDRTr]$/.test(f)) tk.shift(); } continue; }
      if (l === 'env') { tk.shift(); while (tk.length && (/^-/.test(tk[0]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(tk[0]))) { const f = tk.shift(); if (/^-[uC]$/.test(f)) tk.shift(); } continue; }
      if (['time', 'command', 'nohup', 'exec', 'builtin', 'then', 'do', 'else', 'elif', 'if', 'while', 'until', 'call'].includes(l)) { tk.shift(); continue; }
      break;
    }
    if (!tk.length) continue;
    const verb = baseName(tk[0].replace(/^[$({!&]+/, ''));
    if (CD_VERBS.has(verb)) { const t = tk.slice(1).find((a) => !/^-/.test(a) && !/^\/d$/i.test(a)); if (t) chain.push(t); continue; }
    if ((EVALS.has(verb) || SHELLS.has(verb)) && depth < 3) {
      let text = null;
      if (EVALS.has(verb)) text = tk.slice(1).join(' ');
      else {
        const idx = tk.findIndex((a, i) => i > 0 && /^(-[A-Za-z]*c|-command|\/[ck])$/i.test(a));
        if (idx > 0) { const rest = tk.slice(idx + 1); text = rest.length === 1 ? rest[0] : rest.join(' '); }
      }
      if (text && /git/i.test(text)) findGitCommit(text, isPS || /^(pwsh|powershell)$/.test(verb), aliasOf, depth + 1, acc, chain);
      continue;
    }
    if (verb !== 'git') continue;
    tk.shift();
    const gitC = [];
    let i = 0;
    while (i < tk.length && /^-/.test(tk[i])) {
      if (GIT_OPT_VALUE.has(tk[i])) { if (tk[i] === '-C' && tk[i + 1]) gitC.push(tk[i + 1]); i += 2; } else i += 1;
    }
    let sub = tk[i];
    if (sub === 'add' || sub === 'stage') {
      const add = { force: false, allStyle: false, paths: [], cdirs: [...chain, ...gitC] };
      let end = false;
      for (let k = i + 1; k < tk.length; k++) {
        const t = tk[k];
        if (end) { add.paths.push(t); continue; }
        if (t === '--') { end = true; continue; }
        if (t.startsWith('--')) {
          const name = t.slice(2).split('=')[0];
          if (name === 'force') add.force = true;
          if (['all', 'update', 'patch', 'interactive', 'edit', 'pathspec-from-file'].includes(name)) add.allStyle = true;
          continue;
        }
        if (t.startsWith('-') && t.length > 1) { if (/f/.test(t)) add.force = true; if (/[Auipe]/.test(t)) add.allStyle = true; continue; }
        if (t === '.' || t === ':/' || t === '*' || /^:\(top\)/.test(t)) add.allStyle = true;
        add.paths.push(t);
      }
      acc.adds.push(add);
      continue;
    }
    let aliasExtra = [];
    if (sub && sub !== 'commit') {
      let al = null; try { al = aliasOf(sub); } catch { al = null; }
      if (al && /^(?:!\s*git\s+)?commit(?:\s|$)/.test(String(al).trim())) { sub = 'commit'; aliasExtra = String(al).trim().replace(/^!\s*git\s+/, '').split(/\s+/).slice(1); }
    }
    if (sub !== 'commit') continue;
    const cdirs = [...chain, ...gitC];
    acc.found = true; acc.cdirs.push(...cdirs);
    let end = false;
    const rest = [...aliasExtra, ...tk.slice(i + 1)];
    for (let k = 0; k < rest.length; k++) {
      const t = rest[k];
      if (end) { acc.paths.push(t); continue; }
      if (t === '--') { end = true; continue; }
      if (t.startsWith('--')) {
        const name = t.slice(2).split('=')[0];
        if (name === 'all') acc.all = true;
        if (name === 'message' || name === 'file') { const v = t.includes('=') ? t.slice(t.indexOf('=') + 1) : rest[++k]; if (v !== undefined) (name === 'message' ? acc.messages : acc.msgFiles).push(v); continue; }
        if (COMMIT_LONG_VALUE.has(name) && !t.includes('=')) k++;
        continue;
      }
      if (t.startsWith('-') && t.length > 1) {
        for (let x = 1; x < t.length; x++) {
          const ch = t[x];
          if (ch === 'a') acc.all = true;
          if (COMMIT_SHORT_VALUE.has(ch)) {
            const attached = t.slice(x + 1);
            const v = attached !== '' ? attached : rest[++k];
            if (v !== undefined && ch === 'm') acc.messages.push(v);
            if (v !== undefined && ch === 'F') acc.msgFiles.push(v);
            break;
          }
        }
        continue;
      }
      acc.paths.push(t);
    }
  }
  return acc;
}

// ---------- varredura de um buffer ----------
// devolve { lines: [[nLinha, nomeDoPadrao], ...], note: null | 'utf16' | 'binary' }
function scanBuffer(buf, nameHasKw, patterns) {
  let text; let note = null;
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) { text = buf.subarray(2).toString('utf16le'); note = 'utf16'; }
  else if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) { text = Buffer.from(buf.subarray(2)).swap16().toString('utf16le'); note = 'utf16'; }
  else if (buf.subarray(0, 8000).includes(0)) {
    const s = buf.subarray(0, Math.min(buf.length, 400));
    let odd = 0; let even = 0; const pairs = Math.floor(s.length / 2);
    for (let i = 0; i + 1 < s.length; i += 2) { if (s[i] === 0) even++; if (s[i + 1] === 0) odd++; }
    if (pairs >= 4 && odd / pairs > 0.3 && even / pairs < 0.05) { text = buf.toString('utf16le'); note = 'utf16'; }
    else if (pairs >= 4 && even / pairs > 0.3 && odd / pairs < 0.05) { text = Buffer.from(buf).swap16().toString('utf16le'); note = 'utf16'; }
    else { text = buf.toString('latin1'); note = 'binary'; }
  } else text = buf.toString('utf8');
  const lines = [];
  text.split(/\r\n|\r|\n/).forEach((line, i) => {
    for (const [name, re, needsCtx] of patterns) {
      if (!re.test(line)) continue;
      if (needsCtx && !(KEYWORD.test(line) || nameHasKw)) continue;
      lines.push([i + 1, name]);
    }
  });
  return { lines, note };
}

// ---------- main ----------
function main() {
  const argv = process.argv.slice(2);
  const ALL = argv.includes('--all');
  const STAGED = argv.includes('--staged');
  let commit = { found: true, all: false, paths: [], cdirs: [], adds: [], messages: [], msgFiles: [] };
  let jsonCwd = null;
  let aliasCwd = null;
  if (!ALL && !STAGED) {
    let stdin = '';
    if (!process.stdin.isTTY) { try { stdin = readFileSync(0, 'utf8'); } catch { stdin = ''; } }
    if (stdin.trim()) {
      let j;
      try { j = JSON.parse(stdin); } catch { warn('entrada JSON invalida (fail-open)'); return 0; }
      if (typeof j?.cwd === 'string' && j.cwd.trim()) jsonCwd = j.cwd.trim();
      aliasCwd = jsonCwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();
      const aliasOf = (name) => {
        if (!/^[A-Za-z0-9_.-]+$/.test(name)) return null;
        try { return execFileSync('git', ['config', '--get', `alias.${name}`], { cwd: aliasCwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim() || null; } catch { return null; }
      };
      const info = findGitCommit(String(j?.tool_input?.command ?? ''), j?.tool_name === 'PowerShell', aliasOf);
      if (!info.found) return 0;
      commit = info;
    }
  }

  // raiz do repositorio (funciona em subdiretorio; tenta cwd do JSON / CLAUDE_PROJECT_DIR e depois o cwd do processo)
  // uso manual/git pre-commit (--staged/--all): o cwd do processo e o repositorio em que o commit esta ocorrendo; no hook do harness, CLAUDE_PROJECT_DIR/cwd do JSON
  const base0 = (ALL || STAGED) ? process.cwd() : (jsonCwd ? path.resolve(process.env.CLAUDE_PROJECT_DIR || process.cwd(), jsonCwd) : (process.env.CLAUDE_PROJECT_DIR || process.cwd()));
  const resolveCwd = (cdirs) => { let s = base0; for (const c of cdirs) s = path.resolve(s, c); return s; };
  const start = resolveCwd(commit.cdirs);
  let top = null; let cmdCwd = start;
  for (const cand of [start, process.cwd()]) {
    try { top = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: cand, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim(); cmdCwd = cand; break; } catch { /* tenta o proximo */ }
  }
  if (!top) { warn('nao foi possivel localizar um repositorio git (fail-open: nada foi varrido)'); return 0; }

  const gitBuf = (args, cwd = top) => execFileSync('git', args, { cwd, maxBuffer: 1024 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });
  const list = (args, cwd = top) => gitBuf(args, cwd).toString('utf8').split('\0').filter(Boolean);
  const entries = new Map(); // arquivo -> 'index' | 'worktree'
  const blobOf = new Map(); // arquivo (do indice) -> sha do blob
  const gitlinks = new Set(); // submodulos no indice
  try {
    if (ALL) for (const f of list(['ls-files', '-z'])) entries.set(f, 'worktree');
    else {
      // UM processo git para listar o indice: `diff --cached --raw -z` traz modo e sha do blob (o conteudo vem depois, em lote, por `git cat-file --batch`)
      const raw = list(['diff', '--cached', '--raw', '-z', '--no-abbrev', '--no-renames', '--diff-filter=ACMR']);
      for (let i = 0; i + 1 < raw.length; i += 2) {
        const m = /^:(\d+) (\d+) ([0-9a-f]+) ([0-9a-f]+) [A-Z]/.exec(raw[i]);
        if (!m) throw new Error('saida inesperada de git diff --raw');
        entries.set(raw[i + 1], 'index'); blobOf.set(raw[i + 1], m[4]); if (m[2] === '160000') gitlinks.add(raw[i + 1]); // 160000 = submodulo (gitlink): sem conteudo
      }
      if (commit.all) for (const f of list(['diff', '--name-only', '--diff-filter=ACMR', '-z'])) entries.set(f, 'worktree');
      if (commit.paths.length) for (const f of list(['ls-files', '-z', '--full-name', '--', ...commit.paths], cmdCwd)) entries.set(f, 'worktree');
      // `git add ...` no MESMO comando, antes do commit: o que o add incluiria (ainda nao esta no indice neste momento)
      for (const a of commit.adds) {
        const acwd = resolveCwd(a.cdirs);
        const untracked = (extra) => list(['ls-files', '-z', '--full-name', '--others', '--exclude-standard', ...extra], acwd);
        const ignored = (extra) => list(['ls-files', '-z', '--full-name', '--others', '--ignored', '--exclude-standard', ...extra], acwd);
        const modified = (extra) => list(['diff', '--name-only', '--diff-filter=ACMR', '-z', ...extra], acwd);
        if (!a.allStyle && !a.paths.length) continue; // `git add` sem caminho nao inclui nada
        const ps = a.allStyle ? [] : ['--', ...a.paths]; // -A/./-u/-p: arvore inteira (superconjunto conservador); senao so os caminhos nomeados
        const targets = [...modified(ps), ...untracked(ps)];
        if (a.force) targets.push(...ignored(ps));
        for (const f of targets) if (!entries.has(f)) entries.set(f, 'worktree');
      }
    }
  } catch { console.error('BLOQUEADO pelo secret-scan (fail-closed): nao foi possivel listar os arquivos do commit (git falhou). Resolva e tente de novo.'); return 2; }

  const extra = compileList('EXTRA_SECRET_PATTERNS').map(([i, re]) => [`padrao extra #${i}`, re]);
  const allowUser = compileList('SECRET_ALLOW_FILES').map(([, re]) => re);
  const patterns = [...PATTERNS, ...extra];

  // conteudo dos blobs do indice em LOTE (um `git cat-file --batch` por ate 500 blobs distintos), em vez de um `git show :<arquivo>` por arquivo (~60 ms cada no Windows)
  const blobBuf = new Map(); // sha -> Buffer | null (nao lido)
  const shas = [...new Set([...entries].filter(([f, s]) => s === 'index' && !gitlinks.has(f)).map(([f]) => blobOf.get(f)))];
  for (let i = 0; i < shas.length; i += 500) {
    const chunk = shas.slice(i, i + 500);
    let out = null;
    try { out = execFileSync('git', ['cat-file', '--batch'], { cwd: top, input: `${chunk.join('\n')}\n`, maxBuffer: 1024 * 1024 * 1024, stdio: ['pipe', 'pipe', 'pipe'] }); } catch { out = null; }
    let pos = 0;
    for (const sha of chunk) {
      let got = null;
      if (out) {
        const nl = out.indexOf(10, pos);
        const m = nl < 0 ? null : /^([0-9a-f]+) (\w+) (\d+)$/.exec(out.subarray(pos, nl).toString('latin1'));
        if (m && m[1] === sha) { const size = Number(m[3]); if (nl + 1 + size <= out.length) got = out.subarray(nl + 1, nl + 1 + size); pos = nl + 1 + size + 1; }
        else { out = null; } // desalinhado ("missing" ou saida inesperada): os restantes ficam como nao lidos (fail-closed)
      }
      blobBuf.set(sha, got);
    }
  }
  const hits = []; const unreadable = []; const utf16 = []; const binaries = [];
  for (const [f, src] of entries) {
    const fs_ = f.replace(/\\/g, '/');
    if (allowUser.some((re) => re.test(fs_))) continue;
    if (ENV_FILE.test(fs_)) hits.push([f, 0, 'arquivo .env versionado']);
    if (CRED_FILE.test(fs_)) hits.push([f, 0, 'arquivo de credencial/chave (por nome)']);
    if (BUILTIN_ALLOW.test(fs_)) continue;
    let buf;
    if (src === 'index') {
      if (gitlinks.has(f)) continue; // gitlink: nao ha conteudo a ler
      buf = blobBuf.get(blobOf.get(f));
      if (!buf) { unreadable.push(f); continue; }
    } else {
      try { buf = readFileSync(path.join(top, f)); }
      catch (e) {
        if (e && e.code === 'ENOENT') continue; // deletado no working tree
        unreadable.push(f); continue;
      }
    }
    const r = scanBuffer(buf, KEYWORD.test(fs_), patterns);
    if (r.note === 'utf16') utf16.push(f); else if (r.note === 'binary') binaries.push(f);
    for (const [ln, name] of r.lines) hits.push([f, ln, name]);
  }
  // mensagem do commit (-m/--message) e arquivo de mensagem (-F/--file)
  commit.messages.forEach((m, i) => { const r = scanBuffer(Buffer.from(String(m), 'utf8'), false, patterns); for (const [ln, name] of r.lines) hits.push([`<mensagem do commit #${i + 1}>`, ln, name]); });
  for (const mf of commit.msgFiles) {
    if (mf === '-') continue;
    try { const r = scanBuffer(readFileSync(path.resolve(cmdCwd, mf)), false, patterns); for (const [ln, name] of r.lines) hits.push([`<arquivo de mensagem ${mf}>`, ln, name]); }
    catch { unreadable.push(`<arquivo de mensagem ${mf}>`); }
  }
  const names = (a) => `${a.slice(0, 5).join(', ')}${a.length > 5 ? ` (+${a.length - 5})` : ''}`;
  if (utf16.length) warn(`arquivo(s) UTF-16 decodificado(s) e varrido(s): ${names(utf16)}`);
  if (binaries.length) warn(`arquivo(s) binario(s) varrido(s) SO como texto latino (segredo em formato binario, ex.: .pfx, nao e detectavel): ${names(binaries)}`);
  let code = 0;
  if (unreadable.length) {
    console.error('BLOQUEADO pelo secret-scan (fail-closed): nao foi possivel ler estes arquivos do commit, entao nao da para garantir que estao limpos:');
    for (const f of unreadable) console.error(`  ${f}`);
    code = 2;
  }
  if (hits.length) {
    console.error('BLOQUEADO pelo secret-scan (valores NAO exibidos):');
    for (const [f, l, n] of hits) console.error(`  ${f}:${l} — ${n}`);
    console.error('Remova do indice, rotacione o segredo se ele existiu em algum commit e escale ao owner (Constitution §10).');
    code = 2;
  }
  return code;
}

try { process.exitCode = main(); }
catch { console.error('BLOQUEADO pelo secret-scan (fail-closed): erro interno durante a varredura; nada foi aprovado. Rode `node secret-scan.mjs --staged` manualmente para diagnosticar.'); process.exitCode = 2; }
