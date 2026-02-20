/**
 * Compute a human-friendly display label for any entity.
 *
 * Special cases:
 *   Account    → "service - username"   (e.g. "google - monaco.marco83@gmail.com")
 *   Credential → "service"              (e.g. "banca_sella_client_id")
 *   Preference → "subject: value"       (e.g. "lingua: italiano")
 *
 * Generic fallback chain: name → title → subject → service → id
 */
export function entityLabel(entity) {
  if (!entity) return '';
  const p = entity.properties || {};
  const type = entity.type || '';

  if (type === 'Account') {
    const svc = p.service || '';
    const user = p.username || p.url || '';
    if (svc && user) return `${svc} - ${user}`;
    if (svc) return svc;
  }

  if (type === 'Credential') {
    return p.service || p.scope || entity.id || '';
  }

  if (type === 'Preference') {
    if (p.subject && p.value) return `${p.subject}: ${p.value}`;
  }

  return p.name || p.title || p.subject || p.service || entity.id || '';
}

/**
 * Same logic but works on a flat properties dict + type + id
 * (used in normalizeRelation where we don't have the full entity wrapper).
 */
export function propsLabel(props, type, id) {
  if (!props) return id || '';

  if (type === 'Account') {
    const svc = props.service || '';
    const user = props.username || props.url || '';
    if (svc && user) return `${svc} - ${user}`;
    if (svc) return svc;
  }

  if (type === 'Credential') {
    return props.service || props.scope || id || '';
  }

  if (type === 'Preference') {
    if (props.subject && props.value) return `${props.subject}: ${props.value}`;
  }

  return props.name || props.title || props.subject || props.service || id || '';
}
