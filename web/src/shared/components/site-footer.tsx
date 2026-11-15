export function SiteFooter() {
  return (
    <footer className="mx-auto flex w-full max-w-[1360px] flex-wrap items-center justify-end gap-1.5 whitespace-nowrap px-4 py-4 text-right text-[11px] text-muted-foreground/70 sm:px-6 lg:px-8">
      <a className="transition-colors hover:text-foreground" href="https://github.com/chenyme/grok2api" target="_blank" rel="noreferrer">Grok2API</a>
      <span>© 2026</span>
      <span aria-hidden="true">·</span>
      <span>Built by</span>
      <a className="transition-colors hover:text-foreground" href="https://blog.cheny.me/" target="_blank" rel="noreferrer">Chenyme</a>
      <span aria-hidden="true">·</span>
      <span>Customized by</span>
      <a className="transition-colors hover:text-foreground" href="https://github.com/LXXYSLF/grok-account-manager-main" target="_blank" rel="noreferrer">Grok 2api</a>
    </footer>
  );
}
