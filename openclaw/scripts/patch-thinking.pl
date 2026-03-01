#!/usr/bin/env perl
# patch-thinking.pl — Applica il patch per-model thinkingDefault a un file JS di OpenClaw.
# Uso: perl patch-thinking.pl <file.js>
use strict;
use warnings;

my $file = $ARGV[0] or die "Uso: perl patch-thinking.pl <file.js>\n";
my $marker = 'per-model thinkingDefault patch';

# Leggi il file
open(my $fh, '<', $file) or die "Cannot open $file: $!\n";
my $content = do { local $/; <$fh> };
close($fh);

# Check se già patchato
if ($content =~ /\Q$marker\E/) {
    print "  Già patchato, skip.\n";
    exit 0;
}

my $changed = 0;

# --- Patch 1: Zod schema ---
# Aggiunge thinkingDefault al models z.object({...}).strict()
# Pattern: streaming: z.boolean().optional() seguito da }).strict() nel blocco models
if ($content =~ s{
    (models:\s*z\.record\(z\.string\(\),\s*z\.object\(\{  # apertura models schema
      .*?                                                   # contenuto
      streaming:\s*z\.boolean\(\)\.optional\(\))            # ultimo campo
    (\s*\}\)\.strict\(\))                                   # chiusura
}{
    $1,\n\t\t/* $marker */thinkingDefault: z.union([z.literal("off"),z.literal("minimal"),z.literal("low"),z.literal("medium"),z.literal("high"),z.literal("xhigh")]).optional()$2
}xs) {
    print "  Patch 1/2 (Zod schema): OK\n";
    $changed++;
} else {
    print "  Patch 1/2 (Zod schema): pattern non trovato (potrebbe non servire)\n";
}

# --- Patch 2: resolveThinkingDefault ---
# Inserisce lookup per-model prima del check globale
if ($content =~ s{
    (function\s+resolveThinkingDefault\(params\)\s*\{)\s*\n
    (\s*const\s+configured)
}{
    $1\n\t/* $marker */ const _m = params.cfg.agents?.defaults?.models ?? {}; for (const [_k, _e] of Object.entries(_m)) { if (_e?.thinkingDefault) { const _p = parseModelRef(_k, params.provider); if (_p && _p.provider === params.provider && _p.model === params.model) return _e.thinkingDefault; } }\n$2
}xs) {
    print "  Patch 2/2 (resolveThinkingDefault): OK\n";
    $changed++;
} else {
    print "  Patch 2/2 (resolveThinkingDefault): FALLITO\n";
    exit 1;
}

if ($changed > 0) {
    open(my $wfh, '>', $file) or die "Cannot write $file: $!\n";
    print $wfh $content;
    close($wfh);
    print "  File salvato.\n";
}
