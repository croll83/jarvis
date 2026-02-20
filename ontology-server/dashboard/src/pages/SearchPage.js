import React, { useState, useEffect } from 'react';
import styled from 'styled-components';
import { useSearchParams } from 'react-router-dom';
import MiniEntityCard from '../components/MiniEntityCard';
import Breadcrumb from '../components/Breadcrumb';
import { ShimmerGrid } from '../components/LoadingShimmer';
import { searchEntities, fetchEntities } from '../services/api';
import { entityLabel } from '../utils/labels';

const Wrap = styled.div`padding: 24px; max-width: 1200px; margin: 0 auto; width: 100%;`;
const Title = styled.h2`font-size: 1.2rem; margin-bottom: 20px; color: ${({ theme }) => theme.colors.textSecondary};
  span { color: ${({ theme }) => theme.colors.text}; font-weight: 700; }
`;
const Grid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
`;
const Empty = styled.div`padding: 40px; text-align: center; color: ${({ theme }) => theme.colors.textMuted};`;

export default function SearchPage() {
  const [params] = useSearchParams();
  const q = params.get('q') || '';
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!q) return;
    setLoading(true);
    searchEntities({ q, limit: 50 })
      .then((r) => setResults(Array.isArray(r.data) ? r.data : r.data?.results || []))
      .catch(() => {
        // fallback: client-side filter
        fetchEntities().then((r) => {
          const all = Array.isArray(r.data) ? r.data : r.data?.entities || [];
          const lq = q.toLowerCase();
          setResults(all.filter((e) => {
            const name = entityLabel(e).toLowerCase();
            return name.includes(lq);
          }));
        }).catch(() => setResults([]));
      })
      .finally(() => setLoading(false));
  }, [q]);

  return (
    <>
      <Breadcrumb items={[
        { label: 'Entities', path: '/' },
        { label: `Search: ${q}`, path: `/search?q=${q}` },
      ]} />
      <Wrap>
        <Title>Results for <span>"{q}"</span> ({results.length})</Title>
        {loading ? <ShimmerGrid count={6} /> : results.length === 0 ? (
          <Empty>No results found for "{q}"</Empty>
        ) : (
          <Grid>
            {results.map((e, i) => <MiniEntityCard key={e.id} entity={e} index={i} />)}
          </Grid>
        )}
      </Wrap>
    </>
  );
}
