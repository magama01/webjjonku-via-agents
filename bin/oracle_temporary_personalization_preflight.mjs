#!/usr/bin/env node
import path from 'node:path';
import childProcess from 'node:child_process';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const jsonAt = async (port, resource) => {
  const response = await fetch(`http://127.0.0.1:${port}/json/${resource}`, { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(`Chrome endpoint returned ${response.status}`);
  return response.json();
};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const isBlank = target => ['about:blank', 'chrome://newtab/', 'chrome://new-tab-page/']
  .includes(String(target.url ?? '').trim().toLowerCase());

export async function stopOwnedLauncher(launcher) {
  const child = launcher?.chromeProcess;
  let timer;
  const stopped = child && child.exitCode === null && child.signalCode === null
    ? new Promise(resolve => {
      child.once('close', resolve);
      timer = setTimeout(resolve, 10000);
    }) : Promise.resolve();
  try { await Promise.resolve().then(() => launcher?.kill()); await stopped; }
  finally { clearTimeout(timer); }
}

async function dependenciesFor(packageRoot, helperPath) {
  const moduleAt = relative => import(pathToFileURL(path.join(packageRoot, relative)).href);
  const requireFromOracle = createRequire(path.join(packageRoot, 'package.json'));
  return {
    lifecycle: await moduleAt('dist/src/browser/chromeLifecycle.js'),
    ...await moduleAt('dist/src/browser/actions/navigation.js'),
    ...await import(pathToFileURL(requireFromOracle.resolve('chrome-launcher')).href),
    ...await import(pathToFileURL(helperPath).href),
    jsonAt, pause, frontmostApp, activateApp,
  };
}

// macOS activates a freshly launched Chrome for a moment even when its window
// starts off-screen. Remember the user's frontmost app and hand focus back.
const osascript = script => new Promise(resolve => {
  childProcess.execFile('osascript', ['-e', script], { timeout: 3000 }, (error, stdout) => {
    resolve(error ? null : String(stdout).trim());
  });
});
async function frontmostApp() {
  if (process.platform !== 'darwin') return null;
  return osascript('tell application "System Events" to get name of first application process whose frontmost is true');
}
async function activateApp(name) {
  if (process.platform !== 'darwin' || !name) return false;
  const escaped = String(name).replace(/["\\]/g, '\\$&');
  return (await osascript(`tell application "System Events" to set frontmost of process "${escaped}" to true`)) !== null;
}

const projectUrlPattern = /^https:\/\/chatgpt\.com\/g\/g-p-[A-Za-z0-9_-]+\/project\/?$/;
const normalizeProjectUrl = value => {
  const parsed = new URL(String(value ?? '').trim());
  const normalized = `${parsed.origin}${parsed.pathname.replace(/\/$/, '')}`;
  if (parsed.origin !== 'https://chatgpt.com' || !projectUrlPattern.test(normalized)) {
    const error = new Error('ChatGPT Project URL could not be confirmed');
    error.code = 'WORKSPACE_PROJECT_URL_UNCONFIRMED';
    throw error;
  }
  return normalized;
};
const codedError = (code, message) => Object.assign(new Error(message), { code });

export function isWorkspaceProjectBootstrapHomeUrl(value) {
  try {
    const url = new URL(String(value ?? ''));
    return url.origin === 'https://chatgpt.com' && url.pathname === '/';
  } catch {
    return false;
  }
}

export function selectWorkspaceProjectCreateTrigger(root) {
  const visible = element => Boolean(element?.isConnected && element.getClientRects?.().length > 0);
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const lower = value => clean(value).toLocaleLowerCase();
  const text = element => clean(element?.getAttribute?.('aria-label') || element?.getAttribute?.('title') || element?.innerText || element?.textContent);
  const controls = [...root.querySelectorAll('button,[role="button"],[role="menuitem"]')].filter(visible);
  const exactLabels = new Set(['New project', 'Create project', '새 프로젝트', '프로젝트 만들기', '프로젝트 생성'].map(lower));
  const exact = controls.filter(element => exactLabels.has(lower(text(element))));
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return null;

  const sidebarAncestor = element => {
    for (let current = element?.parentElement; current; current = current.parentElement) {
      const tag = lower(current.tagName);
      const role = lower(current.getAttribute?.('role'));
      const marker = lower([
        current.getAttribute?.('data-testid'), current.getAttribute?.('id'), current.getAttribute?.('aria-label'),
      ].filter(Boolean).join(' '));
      if (tag === 'nav' || tag === 'aside' || role === 'navigation' || /sidebar|side bar|navigation|nav/.test(marker)) return current;
      if (current === root) break;
    }
    return null;
  };
  const isAuxiliaryCandidate = element => {
    if (!visible(element) || lower(element.tagName) !== 'button') return false;
    if (element.disabled || element.getAttribute?.('aria-disabled') === 'true') return false;
    if (element.getAttribute?.('aria-expanded') !== null || element.getAttribute?.('aria-haspopup') !== null) return false;
    const label = lower(text(element));
    if (/^(new chat|start new chat|새 채팅|새로운 채팅)$/.test(label)) return false;
    if (label) return /project|프로젝트|add|추가|new|새|create|만들기|생성/.test(label) && !/chat|채팅/.test(label);
    return Boolean(element.querySelector?.('svg,[data-icon],[class*="icon"]'));
  };
  const headings = [...root.querySelectorAll('div,span,p,h2,h3')].filter(element =>
    visible(element) && ['projects', '프로젝트'].includes(lower(text(element))));
  const candidates = [];
  for (const heading of headings) {
    const sidebar = sidebarAncestor(heading);
    if (!sidebar) continue;
    let scope = heading.parentElement;
    for (let depth = 0; depth < 3 && scope && scope !== sidebar; depth += 1, scope = scope.parentElement) {
      const scoped = [...scope.querySelectorAll('button')].filter(isAuxiliaryCandidate);
      if (scoped.length > 1) return null;
      if (scoped.length === 1) {
        candidates.push(scoped[0]);
        break;
      }
      if (scope === sidebar) break;
    }
  }
  const unique = [...new Set(candidates)];
  return unique.length === 1 ? unique[0] : null;
}

export async function ensureWorkspaceProject(Runtime, bootstrap, logger = () => {}) {
  const name = String(bootstrap?.name ?? '').trim();
  const instructions = String(bootstrap?.instructions ?? '').trim();
  if (!name || !instructions) throw codedError('WORKSPACE_PROJECT_BOOTSTRAP_INVALID', 'workspace Project name/instructions are required');
  const expression = `(${bootstrapWorkspaceProject.toString()})(${JSON.stringify(name)}, ${JSON.stringify(instructions)}, ${selectWorkspaceProjectCreateTrigger.toString()}, ${isWorkspaceProjectBootstrapHomeUrl.toString()})`;
  const result = await Runtime.evaluate({ expression, awaitPromise: true, returnByValue: true });
  const value = result.result?.value;
  if (result.exceptionDetails || !value?.ok) {
    const code = String(value?.code ?? 'WORKSPACE_PROJECT_CREATE_FAILED');
    throw codedError(code, String(value?.error ?? result.exceptionDetails?.exception?.description ?? result.exceptionDetails?.text ?? 'workspace Project initialization failed'));
  }
  const url = normalizeProjectUrl(value.project_url);
  logger(`[browser] Workspace Project: ${value.created ? 'created' : 'reused'} ${url}`);
  return { url, created: value.created === true };
}

async function bootstrapWorkspaceProject(name, instructions, selectProjectCreateTrigger, isBootstrapHomeUrl) {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = element => Boolean(element?.isConnected && element.getClientRects().length > 0);
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const lower = value => clean(value).toLocaleLowerCase();
  const text = element => clean(element?.getAttribute?.('aria-label') || element?.getAttribute?.('title') || element?.innerText || element?.textContent);
  const pageLoggedOut = () => location.pathname.startsWith('/auth') || [...document.querySelectorAll('a,button')].some(element =>
    visible(element) && ['log in', 'sign up', '로그인', '회원가입', '가입하기'].includes(lower(text(element))));
  const projectHref = href => {
    try {
      const url = new URL(href, location.origin);
      return url.origin === 'https://chatgpt.com' && /^\/g\/g-p-[A-Za-z0-9_-]+\/project\/?$/.test(url.pathname)
        ? `${url.origin}${url.pathname.replace(/\/$/, '')}` : null;
    } catch { return null; }
  };
  const namedProjectLinks = () => [...document.querySelectorAll('a[href]')].filter(element =>
    visible(element) && projectHref(element.href) && lower(text(element).split('\n')[0]) === lower(name));
  const namedProjectOptions = () => [...document.querySelectorAll('button')].filter(element =>
    visible(element) && lower(element.getAttribute?.('aria-label')) === lower(`Open project options for ${name}`));
  const projectHomeSibling = optionsButton => {
    const parent = optionsButton?.parentElement;
    if (!parent) return null;
    const candidates = [...parent.children].filter(element =>
      element !== optionsButton && lower(element.tagName) === 'button' && visible(element)
      && lower(element.getAttribute?.('aria-label')) === 'open project home');
    return candidates.length === 1 ? candidates[0] : null;
  };
  const click = element => { element.scrollIntoView?.({ block: 'center' }); element.click(); };
  const waitFor = async (probe, message, attempts = 100) => {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const value = probe();
      if (value) return value;
      await sleep(100);
    }
    throw new Error(message);
  };
  const buttonByLabels = (root, labels) => {
    const wanted = new Set(labels.map(lower));
    const candidates = [...root.querySelectorAll('button,[role="button"],[role="menuitem"]')].filter(element =>
      visible(element) && wanted.has(lower(text(element))));
    return candidates.length === 1 ? candidates[0] : null;
  };
  const projectCreateLabels = ['Create project', 'Create', '만들기', '생성', '프로젝트 만들기', '프로젝트 생성'];
  const inputLabel = element => {
    const values = [
      element.getAttribute?.('aria-label'), element.getAttribute?.('title'), element.getAttribute?.('placeholder'),
      element.placeholder, element.getAttribute?.('name'),
    ];
    for (const label of element.labels ? [...element.labels] : []) values.push(text(label));
    const id = element.getAttribute?.('id');
    if (id) {
      for (const label of [...(document.querySelectorAll?.('label') ?? [])]) {
        if (label.getAttribute?.('for') === id) values.push(text(label));
      }
    }
    const labelledBy = clean(element.getAttribute?.('aria-labelledby'));
    if (labelledBy) {
      for (const labelId of labelledBy.split(/\s+/)) values.push(text(document.getElementById?.(labelId)));
    }
    return lower(values.filter(Boolean).join(' '));
  };
  const textInputs = root => [...root.querySelectorAll('input')].filter(element =>
    visible(element) && (!element.type || ['text', 'search'].includes(lower(element.type))));
  const projectCreationCandidate = root => {
    if (!visible(root)) return null;
    const inputs = textInputs(root);
    const related = inputs.filter(element => /project|프로젝트|name|이름/.test(inputLabel(element)));
    if (related.length > 1) return { ambiguous: true };
    const rootRelated = /project|프로젝트/.test(lower(text(root)));
    const input = related.length === 1 ? related[0] : rootRelated && inputs.length === 1 ? inputs[0] : null;
    if (!input) return null;
    const create = buttonByLabels(root, projectCreateLabels);
    if (!create) return null;
    return { root, input, create };
  };
  const projectCreationSurface = () => {
    const dialogRoots = [...document.querySelectorAll('[role="dialog"]')].filter(visible);
    const dialogCandidates = dialogRoots.map(projectCreationCandidate).filter(Boolean);
    if (dialogCandidates.some(candidate => candidate.ambiguous)) return { ambiguous: true };
    if (dialogCandidates.length === 1) return dialogCandidates[0];
    if (dialogCandidates.length > 1) return { ambiguous: true };

    const roots = [...new Set([
      ...document.querySelectorAll('[popover],[role="alertdialog"],[data-radix-popper-content-wrapper],[data-radix-popover-content],[data-headlessui-popover-panel],[data-slot="popover-content"],[data-slot="dialog-content"],[data-testid*="popover"],[data-testid*="overlay"],form'),
    ])].filter(visible);
    const candidates = roots.map(projectCreationCandidate).filter(Boolean);
    if (candidates.some(candidate => candidate.ambiguous)) return { ambiguous: true };
    const byInput = new Map();
    for (const candidate of candidates) {
      const existing = byInput.get(candidate.input);
      if (!existing) byInput.set(candidate.input, candidate);
      else if (existing.root.contains?.(candidate.root)) byInput.set(candidate.input, candidate);
    }
    const unique = [...byInput.values()];
    return unique.length === 1 ? unique[0] : unique.length > 1 ? { ambiguous: true } : null;
  };
  const setControl = (element, value) => {
    element.focus();
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
      if (!setter) throw new Error('editable control has no native value setter');
      setter.call(element, value);
    } else if (element.isContentEditable) {
      element.textContent = value;
    } else {
      throw new Error('editable Project control is unsupported');
    }
    element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  };

  if (pageLoggedOut()) return { ok: false, code: 'WORKSPACE_PROJECT_LOGGED_OUT', error: 'ChatGPT is logged out; Project initialization requires the existing signed-in profile' };
  if (!isBootstrapHomeUrl(location.href)) {
    return { ok: false, code: 'WORKSPACE_PROJECT_URL_UNCONFIRMED', error: 'Project initialization requires the ChatGPT home page' };
  }
  let trigger = null;
  try {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      trigger = selectProjectCreateTrigger(document) ?? trigger;
      if (trigger) break;
      await sleep(100);
    }
    if (!trigger) throw new Error('New Project control is missing or ambiguous');
  } catch (error) {
    return { ok: false, code: 'WORKSPACE_PROJECT_CREATE_FAILED', error: error.message };
  }

  // Existing Projects, including multiple Projects with the same name, are
  // historical/user state. A fresh execution session must always create a new
  // Project instead of resolving by name. Snapshot existing URLs only so a
  // post-create fallback can identify the newly returned Project ID.
  const existingProjectUrls = new Set(
    [...document.querySelectorAll('a[href]')].map(element => projectHref(element.href)).filter(Boolean),
  );
  click(trigger);
  let surface;
  try {
    surface = await waitFor(projectCreationSurface, 'Project creation surface did not appear', 80);
  } catch (error) {
    return { ok: false, code: 'WORKSPACE_PROJECT_CREATE_FAILED', error: error.message };
  }
  if (surface.ambiguous) {
    return { ok: false, code: 'WORKSPACE_PROJECT_CREATE_FAILED', error: 'Project name input is missing or ambiguous' };
  }
  const { input, create } = surface;
  try { setControl(input, name); } catch (error) { return { ok: false, code: 'WORKSPACE_PROJECT_CREATE_FAILED', error: error.message }; }
  if (create.disabled || create.getAttribute('aria-disabled') === 'true') {
    return { ok: false, code: 'WORKSPACE_PROJECT_CREATE_FAILED', error: 'Project create action is unavailable' };
  }
  click(create);
  let projectUrl;
  try {
    projectUrl = await waitFor(() => {
      const current = projectHref(location.href);
      if (current && !existingProjectUrls.has(current)) return current;
      const newlyObserved = [...new Set(
        [...document.querySelectorAll('a[href]')]
          .map(element => projectHref(element.href))
          .filter(url => url && !existingProjectUrls.has(url)),
      )];
      return newlyObserved.length === 1 ? newlyObserved[0] : null;
    }, 'Created Project URL was not confirmed', 160);
  } catch (error) {
    return { ok: false, code: 'WORKSPACE_PROJECT_URL_UNCONFIRMED', error: error.message };
  }
  return { ok: true, project_url: projectUrl, created: true, instructions };
}

export async function ensureWorkspaceProjectInstructions(Runtime, bootstrap, logger = () => {}) {
  const name = String(bootstrap?.name ?? '').trim();
  const instructions = String(bootstrap?.instructions ?? '').trim();
  const expression = `(${writeWorkspaceProjectInstructions.toString()})(${JSON.stringify(name)}, ${JSON.stringify(instructions)})`;
  const result = await Runtime.evaluate({ expression, awaitPromise: true, returnByValue: true });
  const value = result.result?.value;
  const code = String(value?.code ?? 'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED');
  const error = String(value?.error ?? result.exceptionDetails?.exception?.description ?? result.exceptionDetails?.text ?? 'workspace Project instructions were not confirmed as saved');
  if (code === 'WORKSPACE_PROJECT_URL_UNCONFIRMED') throw codedError(code, error);
  if (result.exceptionDetails || !value?.ok || value?.verified !== true) {
    const warning = { code: 'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED', error };
    logger(`[browser] Warning: Workspace Project instructions were not verified; continuing with the exact Project URL: ${error}`);
    return { ok: true, changed: false, verified: false, warning };
  }
  logger(`[browser] Workspace Project instructions: ${value.changed ? 'updated' : 'already-present'} and verified`);
  return value;
}

async function writeWorkspaceProjectInstructions(name, instructions) {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = element => Boolean(element?.isConnected && element.getClientRects().length > 0);
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const lower = value => clean(value).toLocaleLowerCase();
  const text = element => clean(element?.getAttribute?.('aria-label') || element?.getAttribute?.('title') || element?.innerText || element?.textContent);
  const waitFor = async (probe, attempts = 80) => {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const value = probe();
      if (value) return value;
      await sleep(100);
    }
    return null;
  };
  const click = element => { element.scrollIntoView?.({ block: 'center' }); element.click(); };
  const exact = (root, selector, labels) => {
    const wanted = new Set(labels.map(lower));
    const candidates = [...root.querySelectorAll(selector)].filter(element => visible(element) && wanted.has(lower(text(element))));
    return candidates.length === 1 ? candidates[0] : null;
  };
  const setControl = (element, value) => {
    element.focus();
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
      if (!setter) throw new Error('Project instructions control has no native value setter');
      setter.call(element, value);
    } else if (element.isContentEditable) {
      element.textContent = value;
    } else throw new Error('Project instructions control is unsupported');
    element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  };
  if (!/^\/g\/g-p-[A-Za-z0-9_-]+\/project\/?$/.test(location.pathname)) {
    return { ok: false, code: 'WORKSPACE_PROJECT_URL_UNCONFIRMED', error: 'Not on the confirmed ChatGPT Project page' };
  }
  let trigger = exact(document, 'button,[role="button"]', [
    'Add instructions', 'Instructions', 'Project instructions', '지침 추가', '지침', '프로젝트 지침',
  ]);
  if (!trigger) {
    const menuTrigger = [...document.querySelectorAll('button')].filter(element => {
      if (!visible(element)) return false;
      const label = lower(text(element));
      return (element.getAttribute('aria-haspopup') === 'menu' && lower(element.innerText).includes(lower(name)))
        || ['project options', 'project menu', 'more', '프로젝트 옵션', '프로젝트 메뉴', '더보기'].includes(label);
    });
    if (menuTrigger.length === 1) {
      click(menuTrigger[0]);
      const item = await waitFor(() => exact(document, '[role="menuitem"],button', [
        'Edit project', 'Project settings', 'Instructions', '프로젝트 수정', '프로젝트 설정', '지침',
      ]), 40);
      if (item) { click(item); trigger = item; }
    }
  } else click(trigger);
  const editor = await waitFor(() => {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible);
    const roots = dialogs.length ? dialogs : [document];
    const candidates = roots.flatMap(root => [...root.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible));
    const preferred = candidates.filter(element => /instruction|지침/.test(lower(text(element) || element.getAttribute('placeholder'))));
    return preferred.length === 1 ? preferred[0] : candidates.length === 1 ? candidates[0] : null;
  });
  if (!editor) return { ok: false, code: 'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED', error: 'Project instructions editor is missing or ambiguous' };
  const controlValue = element => element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement ? element.value : element.innerText;
  const current = controlValue(editor);
  if (String(current || '').includes(instructions)) return { ok: true, changed: false, verified: true };
  const merged = clean(current) ? `${String(current).trim()}\n\n${instructions}` : instructions;
  try { setControl(editor, merged); } catch (error) { return { ok: false, code: 'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED', error: error.message }; }
  const root = editor.closest('[role="dialog"]') || document;
  const save = exact(root, 'button,[role="button"]', ['Save', 'Done', '저장', '완료']);
  if (!save || save.disabled || save.getAttribute('aria-disabled') === 'true') {
    return { ok: false, code: 'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED', error: 'Project instructions save action is unavailable' };
  }
  click(save);
  const acknowledged = await waitFor(() => {
    if (!editor.isConnected || !visible(editor)) return true;
    if (!String(controlValue(editor) || '').includes(instructions)) return false;
    return save.disabled || save.getAttribute('aria-disabled') === 'true' || !visible(save);
  }, 40);
  if (!acknowledged) {
    return { ok: false, code: 'WORKSPACE_PROJECT_INSTRUCTIONS_FAILED', error: 'Project instructions save was not acknowledged by the UI' };
  }
  return { ok: true, changed: true, verified: true };
}

// The CLI and prompt-free canary share this startup path. Oracle later attaches
// with --browser-tab evidence.target_id instead of creating another tab.
export async function startPersonalizedBrowser(options, dependencies) {
  const { packageRoot, helperPath, profilePath, port = 0, url, profileName, projectBootstrap } = options;
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('invalid CDP port');
  const parsedUrl = new URL(url);
  const bootstrappingProject = Boolean(projectBootstrap);
  const projectPath = /^\/g\/g-p-[A-Za-z0-9_-]+\/project\/?$/.test(parsedUrl.pathname);
  const projectRun = !bootstrappingProject && projectPath && parsedUrl.search === '' && parsedUrl.hash === '' && projectUrlPattern.test(url);
  const temporaryRun = !bootstrappingProject && !projectPath && parsedUrl.searchParams.get('temporary-chat') === 'true';
  if (parsedUrl.origin !== 'https://chatgpt.com') throw new Error('expected a ChatGPT startup URL');
  if (bootstrappingProject) {
    if (!isWorkspaceProjectBootstrapHomeUrl(parsedUrl.href)) {
      throw codedError('WORKSPACE_PROJECT_BOOTSTRAP_INVALID', 'Project bootstrap must start from the ChatGPT home page');
    }
  } else if (projectPath && !projectRun) {
    throw codedError('WORKSPACE_PROJECT_URL_UNCONFIRMED', 'workspace Project execution requires the exact Project URL without query or fragment');
  } else if (!projectRun && !temporaryRun) {
    throw new Error('expected an exact workspace Project URL or a temporary ChatGPT startup URL');
  }
  const deps = dependencies ?? await dependenciesFor(packageRoot, helperPath);
  const logs = [];
  const logger = message => { logs.push(String(message).slice(0, 500)); options.logger?.(message); };
  logger.verbose = false;
  // Opt-in experiment: WEBJJONKU_HEADLESS=1 launches the owned Chrome with
  // --headless=new instead of an off-screen headed window. Default stays headed.
  const headless = (options.headless ?? process.env.WEBJJONKU_HEADLESS) === true
    || (options.headless ?? process.env.WEBJJONKU_HEADLESS) === '1';
  const flags = deps.lifecycle.buildChromeFlagsForTest(headless, undefined, !bootstrappingProject && !headless);
  if (profileName) flags.push(`--profile-directory=${profileName}`);
  flags.push('--hide-crash-restore-bubble');
  const launchOptions = deps.lifecycle.resolveChromeLaunchOptionsForTest(flags, true);
  const previousFront = deps.frontmostApp ? await deps.frontmostApp() : null;
  const launcher = new deps.Launcher({
    ...launchOptions, userDataDir: profilePath, port, startingUrl: url, handleSIGINT: false,
  }, {
    spawn(command, args, spawnOptions) {
      const child = childProcess.spawn(command, args, { ...spawnOptions, detached: true, windowsHide: true });
      child.unref();
      return child;
    },
  });
  let client;
  let deadlineTimer;
  const deadline = new Promise((_, reject) => {
    deadlineTimer = setTimeout(() => reject(new Error('owned browser startup deadline exceeded')),
      options.startupTimeoutMs ?? 85000);
  });
  const start = async () => {
    await launcher.launch();
    const actualPort = Number(launcher.port);
    if ((port && actualPort !== port) || !actualPort || !Number(launcher.pid)) {
      throw new Error('launched Chrome identity does not match the reserved endpoint');
    }
    let target;
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const targets = await deps.jsonAt(actualPort, 'list');
      target = targets.find(item => item.type === 'page' && item.url === url);
      if (target) break;
      await deps.pause(100);
    }
    if (!target) {
      if (projectRun) throw codedError('WORKSPACE_PROJECT_TARGET_UNCONFIRMED', 'owned workspace Project startup target is unavailable');
      throw new Error('owned temporary-chat startup target is unavailable');
    }
    const targetId = target.id ?? target.targetId;
    const connection = await deps.lifecycle.connectToRemoteChromeTarget('127.0.0.1', actualPort, logger, { targetId });
    client = connection.client;
    if (!client?.Runtime || connection.targetId !== targetId) throw new Error('startup tab identity changed');
    // Clean any startup blanks before UI checks can fail, only in this newly
    // launched browser. Never apply this cleanup to an existing user browser.
    await deps.lifecycle.closeBlankChromeTabs(actualPort, logger, '127.0.0.1', {
      excludeTargetIds: [targetId], preserveOneBlank: false,
    });
    const pages = (await deps.jsonAt(actualPort, 'list')).filter(item => item.type === 'page');
    if (pages.length !== 1 || (pages[0].id ?? pages[0].targetId) !== targetId || pages.some(isBlank)) {
      if (projectRun) throw codedError('WORKSPACE_PROJECT_TARGET_UNCONFIRMED', 'expected exactly the owned workspace Project startup tab');
      throw new Error('expected exactly the owned temporary-chat startup tab');
    }
    const platform = options.platform ?? process.platform;
    if (!bootstrappingProject && !headless && platform === 'darwin') {
      await deps.lifecycle.positionChromeWindowOffscreen(client, profilePath, logger);
    }
    await Promise.all([client.Page.enable(), client.Runtime.enable()]);
    await client.Emulation?.setFocusEmulationEnabled({ enabled: true });
    if (previousFront && previousFront !== 'Google Chrome' && deps.activateApp) {
      const current = await deps.frontmostApp();
      if (current && current !== previousFront) {
        logger(`[browser] Restoring focus to ${previousFront} after owned Chrome launch`);
        await deps.activateApp(previousFront);
      }
    }
    const waitForLocation = async (expected, attempts = 160) => {
      for (let attempt = 0; attempt < attempts; attempt += 1) {
        const observed = await client.Runtime.evaluate({ expression: 'location.href', returnByValue: true });
        if (observed.result?.value === expected) return;
        await deps.pause(100);
      }
      throw codedError('WORKSPACE_PROJECT_URL_UNCONFIRMED', `Browser did not reach ${expected}`);
    };
    const waitForBootstrapDocumentReady = async (attempts = 80) => {
      for (let attempt = 0; attempt < attempts; attempt += 1) {
        const observed = await client.Runtime.evaluate({ expression: 'location.href', returnByValue: true });
        if (isWorkspaceProjectBootstrapHomeUrl(observed.result?.value)) {
          const readiness = await client.Runtime.evaluate({ expression: 'document.readyState', returnByValue: true });
          if (['interactive', 'complete'].includes(String(readiness.result?.value ?? ''))) return;
        }
        await deps.pause(100);
      }
      throw codedError('WORKSPACE_PROJECT_URL_UNCONFIRMED', 'ChatGPT home document did not become ready before Project resolution');
    };
    let conversationUrl = url;
    let workspaceProject;
    if (bootstrappingProject) {
      await waitForBootstrapDocumentReady();
      const projectResolver = deps.ensureWorkspaceProject ?? ensureWorkspaceProject;
      const instructionWriter = deps.ensureWorkspaceProjectInstructions ?? ensureWorkspaceProjectInstructions;
      workspaceProject = await projectResolver(client.Runtime, projectBootstrap, logger);
      await client.Page.navigate({ url: workspaceProject.url });
      await waitForLocation(workspaceProject.url);
      const instructionEvidence = await instructionWriter(client.Runtime, projectBootstrap, logger);
      conversationUrl = workspaceProject.url;
      const observedProject = await client.Runtime.evaluate({ expression: 'location.href', returnByValue: true });
      if (observedProject.result?.value !== conversationUrl) {
        throw codedError('WORKSPACE_PROJECT_URL_UNCONFIRMED', 'initialized Project URL changed before capture');
      }
      const version = await deps.jsonAt(actualPort, 'version');
      if (!version.webSocketDebuggerUrl?.startsWith(`ws://127.0.0.1:${actualPort}/devtools/browser/`)) {
        throw new Error('browser CDP identity is unavailable');
      }
      return { chrome: launcher, client, evidence: {
        ok: true, pid: Number(launcher.pid), port: actualPort, target_id: targetId, headless,
        conversation_url: conversationUrl, browser_ws: version.webSocketDebuggerUrl,
        project_url: workspaceProject.url, project_created: workspaceProject.created,
        instructions_verified: instructionEvidence?.verified === true,
        ...(instructionEvidence?.warning ? { instructions_warning: instructionEvidence.warning } : {}),
        startup_blank_tabs: 0, page_count: 1, personalization: 'not-applicable', logs,
      } };
    }
    await deps.ensurePromptReady(client.Runtime, 60000, logger);
    await deps.ensureChatMode(client.Runtime, client.Input, 10000, logger);
    await deps.ensurePromptReady(client.Runtime, 10000, logger);
    if (temporaryRun) {
      // A cold startup can expose the composer before React attaches the menu
      // handlers. Re-read the idempotent control, never retry a submitted prompt.
      for (let attempt = 0; ; attempt += 1) {
        try { await deps.ensureTemporaryChatPersonalization(client.Runtime, logger); break; }
        catch (error) {
          if (attempt >= 2) throw error;
          await deps.pause(250);
        }
      }
    }
    const observed = await client.Runtime.evaluate({ expression: 'location.href', returnByValue: true });
    if (observed.result?.value !== conversationUrl) {
      if (projectRun) throw codedError('WORKSPACE_PROJECT_URL_UNCONFIRMED', 'workspace Project startup URL changed before submission');
      throw new Error('temporary-chat startup URL changed');
    }
    const version = await deps.jsonAt(actualPort, 'version');
    if (!version.webSocketDebuggerUrl?.startsWith(`ws://127.0.0.1:${actualPort}/devtools/browser/`)) {
      throw new Error('browser CDP identity is unavailable');
    }
    return { chrome: launcher, client, evidence: {
      ok: true, pid: Number(launcher.pid), port: actualPort, target_id: targetId, headless,
      conversation_url: conversationUrl, browser_ws: version.webSocketDebuggerUrl,
      ...(projectRun ? { project_url: normalizeProjectUrl(conversationUrl) } : {}),
      startup_blank_tabs: 0, page_count: 1, personalization: projectRun ? 'not-applicable' : 'enabled', logs,
    } };
  };
  try {
    return await Promise.race([start(), deadline]);
  } catch (error) {
    await Promise.resolve().then(() => client?.close()).catch(() => undefined);
    await stopOwnedLauncher(launcher).catch(() => undefined);
    throw error;
  } finally {
    clearTimeout(deadlineTimer);
  }
}

export async function closePersonalizedBrowser(port, expectedWs, targetId, expectedUrl) {
  const version = await jsonAt(port, 'version');
  if (!expectedWs || version.webSocketDebuggerUrl !== expectedWs) {
    throw new Error('refusing to close a browser whose CDP identity changed');
  }
  const socket = new WebSocket(expectedWs);
  await new Promise((resolve, reject) => {
    let finished = false;
    const finish = error => {
      if (finished) return;
      finished = true;
      clearTimeout(timeout);
      socket.close();
      error ? reject(error) : resolve();
    };
    const timeout = setTimeout(() => finish(new Error('browser close timeout')), 5000);
    socket.addEventListener('open', () => socket.send(JSON.stringify({ id: 1, method: 'Target.getTargets' })));
    socket.addEventListener('message', event => {
      const value = JSON.parse(String(event.data));
      if (value.id === 1) {
        const pages = value.result?.targetInfos?.filter(item => item.type === 'page');
        if (value.error || !pages || pages.length !== 1 || pages[0].targetId !== targetId || pages[0].url !== expectedUrl) {
          finish(new Error('refusing to close a browser whose owned tab changed or has additional tabs'));
          return;
        }
        // Read and close through the same verified browser connection.
        socket.send(JSON.stringify({ id: 2, method: 'Browser.close' }));
      }
      if (value.id === 2) finish(value.error ? new Error('Browser.close rejected') : undefined);
    });
    socket.addEventListener('error', () => finish(new Error('browser close failed')));
    socket.addEventListener('close', () => finish(new Error('browser close was not acknowledged')));
  });
  return { ok: true, closed: true };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    if (process.argv[2] === '--close') {
      const [port, ws, target, url] = process.argv.slice(3);
      process.stdout.write(JSON.stringify(await closePersonalizedBrowser(Number(port), ws, target, url)));
    } else {
      const [packageRoot, helperPath, profilePath, port, url, bootstrapJson] = process.argv.slice(2);
      if (!packageRoot || !helperPath || !profilePath || !port || !url) throw new Error('missing preflight arguments');
      let projectBootstrap;
      if (bootstrapJson) {
        try { projectBootstrap = JSON.parse(bootstrapJson); }
        catch { throw codedError('WORKSPACE_PROJECT_BOOTSTRAP_INVALID', 'Project bootstrap payload is invalid JSON'); }
      }
      const session = await startPersonalizedBrowser({
        packageRoot: path.resolve(packageRoot), helperPath: path.resolve(helperPath),
        profilePath: path.resolve(profilePath), port: Number(port), url, projectBootstrap,
      });
      await session.client.close();
      process.stdout.write(JSON.stringify(session.evidence));
    }
  } catch (error) {
    process.stderr.write(JSON.stringify({
      ok: false,
      code: String(error?.code ?? ''),
      error: String(error?.message ?? error),
    }));
    process.exitCode = 2;
  }
}
