import React, { useState, useEffect, useMemo, useCallback } from 'react';
import styled from 'styled-components';
import GraphCanvas from '../components/GraphCanvas';
import EntityCard from '../components/EntityCard';
import RelationPanel from '../components/RelationPanel';
import Breadcrumb from '../components/Breadcrumb';
import { fetchEntities, fetchEntity, fetchRelated, fetchAllRelations, fetchAllEntities } from '../services/api';
import { normalizeRelation } from '../hooks/useEntities';

const EXPAND_GROUP_THRESHOLD = 10;

const Layout = styled.div`
  display: flex;
  flex: 1;
  overflow: hidden;
`;

const GraphArea = styled.div`
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 500px;
`;

const DetailPanel = styled.aside`
  width: 380px;
  flex-shrink: 0;
  background: ${({ theme }) => theme.colors.surface};
  border-left: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  overflow-y: auto;
  padding: 20px;
  animation: fadeIn 200ms ease;
  @media (max-width: 768px) {
    position: fixed;
    top: 60px; right: 0; bottom: 0;
    width: 85vw;
    z-index: 50;
    box-shadow: -4px 0 20px rgba(0,0,0,0.5);
  }
`;

const CloseBtn = styled.button`
  float: right;
  font-size: 1.2rem;
  padding: 4px 8px;
  color: ${({ theme }) => theme.colors.textMuted};
  &:hover { color: ${({ theme }) => theme.colors.text}; }
`;

function mapBulkRelation(r) {
  return {
    relation: r.rel_type,
    source_id: r.from_id,
    target_id: r.to_id,
    source_name: r.from_label,
    source_type: r.from_type,
    target_name: r.to_label,
    target_type: r.to_type,
    properties: r.properties || {},
  };
}

export default function GraphPage({ speaker }) {
  const [mode, setMode] = useState('levels');
  const [baseEntities, setBaseEntities] = useState([]);
  const [bulkRelations, setBulkRelations] = useState([]);
  const [expandedNodes, setExpandedNodes] = useState(new Set());
  const [expandedData, setExpandedData] = useState(new Map());
  const [loading, setLoading] = useState(true);
  const [allEntities, setAllEntities] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [selectedEntity, setSelectedEntity] = useState(null);
  const [selectedRelations, setSelectedRelations] = useState([]);

  // Reset & fetch on speaker change
  useEffect(() => {
    setSelectedId(null);
    setExpandedNodes(new Set());
    setExpandedData(new Map());
    setMode('levels');
    setAllEntities([]);
    setLoading(true);

    Promise.all([
      fetchEntities({ type: 'Person' }),
      fetchAllRelations(),
    ])
      .then(([entRes, relRes]) => {
        const ents = Array.isArray(entRes.data) ? entRes.data : entRes.data?.entities || [];
        setBaseEntities(ents);
        const raw = Array.isArray(relRes.data) ? relRes.data : [];
        setBulkRelations(raw.map(mapBulkRelation));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [speaker]);

  // Total relation counts per entity (for badges)
  const totalRelCounts = useMemo(() => {
    const counts = {};
    bulkRelations.forEach((r) => {
      counts[r.source_id] = (counts[r.source_id] || 0) + 1;
      counts[r.target_id] = (counts[r.target_id] || 0) + 1;
    });
    return counts;
  }, [bulkRelations]);

  // Compute displayed entities & relations
  const { displayedEntities, displayedRelations } = useMemo(() => {
    if (mode === 'orphans') {
      const relatedIds = new Set();
      bulkRelations.forEach((r) => {
        relatedIds.add(r.source_id);
        relatedIds.add(r.target_id);
      });
      const orphans = allEntities.filter((e) => !relatedIds.has(e.id));
      return { displayedEntities: orphans, displayedRelations: [] };
    }

    // Level mode: base Person entities + expanded entities
    const entityMap = new Map();
    baseEntities.forEach((e) => entityMap.set(e.id, e));
    expandedData.forEach(({ entities }) => {
      entities.forEach((e) => { if (!entityMap.has(e.id)) entityMap.set(e.id, e); });
    });

    const visibleIds = new Set(entityMap.keys());
    const relMap = new Map();
    bulkRelations.forEach((r) => {
      if (visibleIds.has(r.source_id) && visibleIds.has(r.target_id)) {
        relMap.set(`${r.source_id}|${r.relation}|${r.target_id}`, r);
      }
    });
    expandedData.forEach(({ relations }) => {
      relations.forEach((r) => {
        if (visibleIds.has(r.source_id) && visibleIds.has(r.target_id)) {
          relMap.set(`${r.source_id}|${r.relation}|${r.target_id}`, r);
        }
      });
    });

    return {
      displayedEntities: Array.from(entityMap.values()),
      displayedRelations: Array.from(relMap.values()),
    };
  }, [mode, baseEntities, bulkRelations, expandedData, allEntities]);

  // Expand a group node (replace aggregate with individual entities)
  const handleExpandGroup = useCallback((groupId) => {
    setExpandedData((prev) => {
      const next = new Map(prev);
      for (const [parentId, data] of next) {
        if (data.groupedTypes?.[groupId]) {
          const group = data.groupedTypes[groupId];
          const newEntities = data.entities.filter((e) => e.id !== groupId).concat(group.entities);
          const newRelations = data.relations
            .filter((r) => r.target_id !== groupId && r.source_id !== groupId)
            .concat(group.relations);
          const { [groupId]: _, ...remainingGroups } = data.groupedTypes;
          next.set(parentId, { entities: newEntities, relations: newRelations, groupedTypes: remainingGroups });
          break;
        }
      }
      return next;
    });
  }, []);

  // Expand a node: fetch related entities, group high-volume types
  const handleExpandNode = useCallback(async (nodeId) => {
    // Group node → expand inline
    if (nodeId.startsWith('_grp_')) {
      handleExpandGroup(nodeId);
      return;
    }
    if (expandedNodes.has(nodeId)) return;

    try {
      const res = await fetchRelated(nodeId);
      const raw = Array.isArray(res.data) ? res.data : res.data?.relations || [];

      const allCurrent = [
        ...baseEntities,
        ...[...expandedData.values()].flatMap((d) => d.entities),
      ];
      const currentEntity = allCurrent.find((e) => e.id === nodeId);

      // Group by entity type
      const byType = {};
      raw.forEach((r) => {
        const e = r.entity;
        if (!e) return;
        if (!byType[e.type]) byType[e.type] = { entities: [], rawRelations: [] };
        byType[e.type].entities.push(e);
        byType[e.type].rawRelations.push(r);
      });

      const newEntities = [];
      const newRelations = [];
      const groupedTypes = {};

      Object.entries(byType).forEach(([type, data]) => {
        const rels = data.rawRelations.map((r) => normalizeRelation(r, nodeId, currentEntity));

        if (data.entities.length <= EXPAND_GROUP_THRESHOLD) {
          // Small group — add individually
          newEntities.push(...data.entities);
          newRelations.push(...rels);
        } else {
          // Large group — create aggregate node
          const groupId = `_grp_${nodeId}_${type}`;
          newEntities.push({
            id: groupId,
            type: type,
            properties: { name: type },
            _isGroup: true,
            _groupCount: data.entities.length,
          });
          // Determine direction from first relation
          const firstRel = rels[0] || {};
          const isIncoming = firstRel.target_id === nodeId;
          newRelations.push({
            source_id: isIncoming ? groupId : nodeId,
            target_id: isIncoming ? nodeId : groupId,
            relation: firstRel.relation || 'related',
            source_name: '', source_type: type,
            target_name: '', target_type: type,
            properties: {},
          });
          groupedTypes[groupId] = { entities: data.entities, relations: rels };
        }
      });

      setExpandedData((prev) => new Map(prev).set(nodeId, { entities: newEntities, relations: newRelations, groupedTypes }));
      setExpandedNodes((prev) => new Set(prev).add(nodeId));
    } catch (err) {
      console.error('Expand failed:', nodeId, err);
    }
  }, [expandedNodes, baseEntities, expandedData, handleExpandGroup]);

  // Collapse a node: remove its expanded data
  const handleCollapseNode = useCallback((nodeId) => {
    setExpandedData((prev) => { const next = new Map(prev); next.delete(nodeId); return next; });
    setExpandedNodes((prev) => { const next = new Set(prev); next.delete(nodeId); return next; });
  }, []);

  // Toggle orphan mode
  const handleToggleOrphans = useCallback(async () => {
    if (mode === 'orphans') { setMode('levels'); return; }
    setMode('orphans');
    if (allEntities.length === 0) {
      setLoading(true);
      try { const ents = await fetchAllEntities(); setAllEntities(ents); } catch {}
      setLoading(false);
    }
  }, [mode, allEntities.length]);

  // Detail panel
  useEffect(() => {
    if (!selectedId) { setSelectedEntity(null); setSelectedRelations([]); return; }
    Promise.all([fetchEntity(selectedId), fetchRelated(selectedId)])
      .then(([eRes, rRes]) => {
        const ent = eRes.data;
        setSelectedEntity(ent);
        const raw = Array.isArray(rRes.data) ? rRes.data : rRes.data?.relations || [];
        setSelectedRelations(raw.map((r) => normalizeRelation(r, selectedId, ent)));
      })
      .catch(() => {});
  }, [selectedId]);

  return (
    <>
      <Breadcrumb items={[
        { label: 'Entities', path: '/' },
        { label: 'Graph View', path: '/graph' },
      ]} />
      <Layout>
        <GraphArea>
          <GraphCanvas
            entities={displayedEntities}
            relations={displayedRelations}
            expandedNodes={expandedNodes}
            totalRelCounts={totalRelCounts}
            onExpandNode={handleExpandNode}
            onCollapseNode={handleCollapseNode}
            onSelectEntity={setSelectedId}
            showOrphans={mode === 'orphans'}
            onToggleOrphans={handleToggleOrphans}
            loading={loading}
          />
        </GraphArea>
        {selectedEntity && (
          <DetailPanel>
            <CloseBtn onClick={() => setSelectedId(null)}>&#x2715;</CloseBtn>
            <EntityCard entity={selectedEntity} />
            <RelationPanel relations={selectedRelations} currentId={selectedId} />
          </DetailPanel>
        )}
      </Layout>
    </>
  );
}
