import React, { useState, useEffect } from 'react';
import styled from 'styled-components';
import { useNavigate } from 'react-router-dom';
import GraphCanvas from '../components/GraphCanvas';
import EntityCard from '../components/EntityCard';
import RelationPanel from '../components/RelationPanel';
import Breadcrumb from '../components/Breadcrumb';
import { fetchEntities, fetchEntity, fetchRelated } from '../services/api';
import { normalizeRelation } from '../hooks/useEntities';

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

export default function GraphPage() {
  const navigate = useNavigate();
  const [entities, setEntities] = useState([]);
  const [allRelations, setAllRelations] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [selectedEntity, setSelectedEntity] = useState(null);
  const [selectedRelations, setSelectedRelations] = useState([]);

  useEffect(() => {
    fetchEntities().then((res) => {
      const ents = Array.isArray(res.data) ? res.data : res.data?.entities || [];
      setEntities(ents);

      // Build entity lookup for name resolution
      const entMap = new Map();
      ents.forEach((e) => entMap.set(e.id, e));

      // Fetch all relations and normalize
      Promise.all(
        ents.map((e) =>
          fetchRelated(e.id)
            .then((r) => {
              const raw = Array.isArray(r.data) ? r.data : r.data?.relations || [];
              return raw.map((rel) => normalizeRelation(rel, e.id, e));
            })
            .catch(() => [])
        )
      ).then((allRels) => {
        const flat = allRels.flat();
        const seen = new Set();
        const deduped = flat.filter((r) => {
          // Deduplicate: same pair + same relation type (regardless of direction)
          const a = r.source_id < r.target_id ? r.source_id : r.target_id;
          const b = r.source_id < r.target_id ? r.target_id : r.source_id;
          const key = `${a}-${b}-${r.relation}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        setAllRelations(deduped);
      });
    }).catch(() => {});
  }, []);

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
            entities={entities}
            relations={allRelations}
            onSelectEntity={setSelectedId}
          />
        </GraphArea>
        {selectedEntity && (
          <DetailPanel>
            <CloseBtn onClick={() => setSelectedId(null)}>✕</CloseBtn>
            <EntityCard entity={selectedEntity} />
            <RelationPanel relations={selectedRelations} currentId={selectedId} />
          </DetailPanel>
        )}
      </Layout>
    </>
  );
}
