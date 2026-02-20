import { useState, useEffect, useCallback } from 'react';
import { fetchEntities, fetchEntity, fetchRelated, searchEntities } from '../services/api';
import { propsLabel } from '../utils/labels';

/**
 * Normalize a relation from API format to the flat format components expect.
 *
 * API returns:  { relation, direction, entity: { id, type, properties }, properties }
 * Components need: { relation, source_id, target_id, source_name, source_type, target_name, target_type, properties }
 */
export function normalizeRelation(r, currentEntityId, currentEntity = null) {
  // If already normalized (has source_id), return as-is
  if (r.source_id) return r;

  const other = r.entity || {};
  const otherProps = other.properties || {};
  const otherName = propsLabel(otherProps, other.type, other.id);
  const otherType = other.type || '';

  const curProps = currentEntity?.properties || {};
  const curType = currentEntity?.type || '';
  const curName = propsLabel(curProps, curType, currentEntityId);

  const isOutgoing = r.direction === 'outgoing';

  return {
    relation: r.relation || r.type || 'related',
    source_id: isOutgoing ? currentEntityId : other.id,
    target_id: isOutgoing ? other.id : currentEntityId,
    source_name: isOutgoing ? curName : otherName,
    source_type: isOutgoing ? curType : otherType,
    target_name: isOutgoing ? otherName : curName,
    target_type: isOutgoing ? otherType : curType,
    properties: r.properties || {},
  };
}

export function useEntities(typeFilter = null) {
  const [entities, setEntities] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async (params = {}) => {
    setLoading(true);
    setError(null);
    try {
      const p = { ...params };
      if (typeFilter) p.type = typeFilter;
      const res = await fetchEntities(p);
      setEntities(Array.isArray(res.data) ? res.data : res.data?.entities || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [typeFilter]);

  useEffect(() => { load(); }, [load]);

  return { entities, loading, error, reload: load };
}

export function useEntity(id) {
  const [entity, setEntity] = useState(null);
  const [relations, setRelations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setLoading(true);
    Promise.all([fetchEntity(id), fetchRelated(id)])
      .then(([eRes, rRes]) => {
        if (cancelled) return;
        const ent = eRes.data;
        setEntity(ent);
        const raw = Array.isArray(rRes.data) ? rRes.data : rRes.data?.relations || [];
        setRelations(raw.map((r) => normalizeRelation(r, id, ent)));
      })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [id]);

  return { entity, relations, loading, error };
}

export function useSearch() {
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);

  const search = useCallback(async (q) => {
    if (!q || q.length < 2) { setResults([]); return; }
    setLoading(true);
    try {
      const res = await searchEntities({ q, limit: 20 });
      setResults(Array.isArray(res.data) ? res.data : res.data?.results || []);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  return { results, loading, search };
}
