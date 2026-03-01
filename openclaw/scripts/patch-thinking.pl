#!/usr/bin/env perl
# patch-thinking.pl — Applica i patch per:
#   1. Per-model thinkingDefault nello schema Zod + resolveThinkingDefault
#   2. thinkingOverride dal plugin hook before_model_resolve
#
# Uso: perl patch-thinking.pl <file.js>
use strict;
use warnings;

my $file = $ARGV[0] or die "Uso: perl patch-thinking.pl <file.js>\n";
my $marker_permodel = 'per-model thinkingDefault patch';
my $marker_hookoverride = 'hook thinkingOverride patch';

# Leggi il file
open(my $fh, '<', $file) or die "Cannot open $file: $!\n";
my $content = do { local $/; <$fh> };
close($fh);

my $changed = 0;

# ═══════════════════════════════════════════════════════════════════
# PATCH 1: Zod schema — aggiunge thinkingDefault al model entry
# ═══════════════════════════════════════════════════════════════════
if ($content !~ /\Q$marker_permodel\E.*Zod/ && $content =~ /models:\s*z\.record\(z\.string\(\),\s*z\.object\(\{/) {
    if ($content =~ s{
        (models:\s*z\.record\(z\.string\(\),\s*z\.object\(\{
          .*?
          streaming:\s*z\.boolean\(\)\.optional\(\))
        (\s*\}\)\.strict\(\))
    }{
        $1,\n\t\t/* $marker_permodel Zod */thinkingDefault: z.union([z.literal("off"),z.literal("minimal"),z.literal("low"),z.literal("medium"),z.literal("high"),z.literal("xhigh")]).optional()$2
    }xs) {
        print "  Patch 1 (Zod schema thinkingDefault): OK\n";
        $changed++;
    } else {
        print "  Patch 1 (Zod schema thinkingDefault): pattern non trovato\n";
    }
}
elsif ($content =~ /\Q$marker_permodel\E.*Zod/) {
    print "  Patch 1 (Zod schema thinkingDefault): già applicato\n";
}

# ═══════════════════════════════════════════════════════════════════
# PATCH 2: resolveThinkingDefault — lookup per-model thinkingDefault
# ═══════════════════════════════════════════════════════════════════
if ($content !~ /\Q$marker_permodel\E(?!.*Zod)/ && $content =~ /function\s+resolveThinkingDefault\(params\)/) {
    if ($content =~ s{
        (function\s+resolveThinkingDefault\(params\)\s*\{)\s*\n
        (\s*const\s+configured)
    }{
        $1\n\t/* $marker_permodel */ const _m = params.cfg.agents?.defaults?.models ?? {}; for (const [_k, _e] of Object.entries(_m)) { if (_e?.thinkingDefault) { const _p = parseModelRef(_k, params.provider); if (_p && _p.provider === params.provider && _p.model === params.model) return _e.thinkingDefault; } }\n$2
    }xs) {
        print "  Patch 2 (resolveThinkingDefault per-model): OK\n";
        $changed++;
    } else {
        print "  Patch 2 (resolveThinkingDefault per-model): pattern non trovato\n";
    }
}
elsif ($content =~ /\Q$marker_permodel\E(?!.*Zod)/) {
    print "  Patch 2 (resolveThinkingDefault per-model): già applicato\n";
}

# ═══════════════════════════════════════════════════════════════════
# PATCH 3: Global store per thinkingOverride dal plugin hook
# Aggiunge _hookThinkingStore (Map) e la funzione setHookThinking()
# a resolveThinkingDefault — controlla il global store prima di tutto.
# ═══════════════════════════════════════════════════════════════════
if ($content !~ /\Q$marker_hookoverride\E.*store/ && $content =~ /function\s+resolveThinkingDefault\(params\)/) {
    # Inserisci il global Map PRIMA della funzione resolveThinkingDefault
    if ($content =~ s{
        (function\s+resolveThinkingDefault\(params\)\s*\{)
    }{
        /* $marker_hookoverride store */ const _hookThinkingStore = globalThis.__openclawHookThinking ?? (globalThis.__openclawHookThinking = new Map());\n$1
    }xs) {
        print "  Patch 3a (global thinkingOverride store): OK\n";
        $changed++;
    } else {
        print "  Patch 3a (global thinkingOverride store): FALLITO\n";
    }
}
elsif ($content =~ /\Q$marker_hookoverride\E.*store/) {
    print "  Patch 3a (global thinkingOverride store): già applicato\n";
}

# ═══════════════════════════════════════════════════════════════════
# PATCH 4: Hook processing — estrarre thinkingOverride e salvarlo
# Dopo "modelId = modelResolveOverride.modelOverride;" aggiunge:
#   if (modelResolveOverride?.thinkingOverride) { globalThis.__openclawHookThinking?.set(params.sessionKey, modelResolveOverride.thinkingOverride); }
# ═══════════════════════════════════════════════════════════════════
if ($content !~ /\Q$marker_hookoverride\E.*extract/ && $content =~ /modelResolveOverride\?\.modelOverride/) {
    # Pattern: if (modelResolveOverride?.modelOverride) {\n\t\t\tmodelId = modelResolveOverride.modelOverride;\n\t\t\tlog...\n\t\t}
    my $count = 0;
    $content =~ s{
        (if\s*\(modelResolveOverride\?\.modelOverride\)\s*\{\s*\n
            \s*modelId\s*=\s*modelResolveOverride\.modelOverride;\s*\n
            \s*log[^;]+;\s*\n
        \s*\})
    }{
        $1\n\t\t/* $marker_hookoverride extract */ if (modelResolveOverride?.thinkingOverride) { (globalThis.__openclawHookThinking ?? (globalThis.__openclawHookThinking = new Map())).set(params.sessionKey || "default", modelResolveOverride.thinkingOverride); }
    }xsg;
    $count = () = $content =~ /\Q$marker_hookoverride\E.*extract/g;
    if ($count > 0) {
        print "  Patch 4 (hook thinkingOverride extract): OK ($count occurrences)\n";
        $changed++;
    } else {
        print "  Patch 4 (hook thinkingOverride extract): FALLITO\n";
    }
}
elsif ($content =~ /\Q$marker_hookoverride\E.*extract/) {
    print "  Patch 4 (hook thinkingOverride extract): già applicato\n";
}

# ═══════════════════════════════════════════════════════════════════
# PATCH 5: Thinking resolution — leggere dal global store
# Modifica: let resolvedThinkLevel = thinkOnce ?? thinkOverride ?? persistedThinking ?? agentCfg?.thinkingDefault;
# In:       let resolvedThinkLevel = thinkOnce ?? thinkOverride ?? (globalThis.__openclawHookThinking?.get(sessionKey || params?.sessionKey || "default")) ?? persistedThinking ?? agentCfg?.thinkingDefault;
# ═══════════════════════════════════════════════════════════════════
if ($content !~ /\Q$marker_hookoverride\E.*resolve/ && $content =~ /let resolvedThinkLevel = thinkOnce/) {
    my $count = 0;
    $content =~ s{
        (let\s+resolvedThinkLevel\s*=\s*)thinkOnce\s*\?\?\s*thinkOverride\s*\?\?\s*persistedThinking\s*\?\?\s*agentCfg\?\.\s*thinkingDefault
    }{
        $1/* $marker_hookoverride resolve */ thinkOnce ?? thinkOverride ?? (globalThis.__openclawHookThinking?.get(sessionKey || params?.sessionKey || "default")) ?? persistedThinking ?? agentCfg?.thinkingDefault
    }xsg;
    $count = () = $content =~ /\Q$marker_hookoverride\E.*resolve/g;
    if ($count > 0) {
        print "  Patch 5 (thinking resolution hook override): OK ($count occurrences)\n";
        $changed++;
        # Cleanup: rimuovi la entry dal global store dopo averla letta (one-shot)
        # Inserisci subito dopo la riga let resolvedThinkLevel
        $content =~ s{
            (/\* \Q$marker_hookoverride\E resolve \*/ thinkOnce \?\? thinkOverride \?\? \(globalThis\.__openclawHookThinking\?\.get\(sessionKey \|\| params\?\.\s*sessionKey \|\| "default"\)\) \?\? persistedThinking \?\? agentCfg\?\.\s*thinkingDefault;)
        }{
            $1\n\t\t/* $marker_hookoverride cleanup */ { const _sk = sessionKey || params?.sessionKey || "default"; if (globalThis.__openclawHookThinking?.has(_sk)) globalThis.__openclawHookThinking.delete(_sk); }
        }xsg;
    } else {
        print "  Patch 5 (thinking resolution hook override): FALLITO\n";
    }
}
elsif ($content =~ /\Q$marker_hookoverride\E.*resolve/) {
    print "  Patch 5 (thinking resolution hook override): già applicato\n";
}

# Salva
if ($changed > 0) {
    open(my $wfh, '>', $file) or die "Cannot write $file: $!\n";
    print $wfh $content;
    close($wfh);
    print "  File salvato ($changed patch applicati).\n";
} else {
    print "  Nessuna modifica necessaria.\n";
}
