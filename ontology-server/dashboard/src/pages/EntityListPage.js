import React, { useState, useEffect, useMemo } from 'react';
import styled from 'styled-components';
import { useNavigate } from 'react-router-dom';
import SearchBox from '../components/SearchBox';
import TypeFilterSidebar from '../components/TypeFilterSidebar';
import MiniEntityCard from '../components/MiniEntityCard';
import Breadcrumb from '../components/Breadcrumb';
import { ShimmerGrid } from '../components/LoadingShimmer';
import { fetchEntities } from '../services/api';
import { entityLabel } from '../utils/labels';

const Layout = styled.div`
  display: flex;
  flex: 1;
  @media (max-width: 768px) { flex-direction: column; }
`;

const Main = styled.main`flex: 1; display: flex; flex-direction: column; min-width: 0;`;

const TopBar = styled.div`
  padding: 16px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  flex-wrap: wrap;
`;

const ViewToggle = styled.div`display: flex; border-radius: ${({ theme }) => theme.radii.sm}; overflow: hidden; border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};`;
const ToggleBtn = styled.button`
  padding: 8px 14px;
  font-size: 0.8rem;
  font-weight: 500;
  background: ${({ $active, theme }) => $active ? theme.colors.accent : theme.colors.surface};
  color: ${({ $active, theme }) => $active ? '#fff' : theme.colors.textSecondary};
  transition: all ${({ theme }) => theme.transitions.fast};
`;

const SortSelect = styled.select`
  padding: 8px 12px;
  font-size: 0.8rem;
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  border-radius: ${({ theme }) => theme.radii.sm};
  color: ${({ theme }) => theme.colors.text};
`;

const Grid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  padding: 20px;
  @media (max-width: 768px) { grid-template-columns: 1fr; }
`;

const Empty = styled.div`
  padding: 60px 20px;
  text-align: center;
  color: ${({ theme }) => theme.colors.textMuted};
  font-size: 0.9rem;
`;

const Count = styled.span`font-size: 0.8rem; color: ${({ theme }) => theme.colors.textSecondary};`;

export default function EntityListPage() {
  const navigate = useNavigate();
  const [entities, setEntities] = useState([]);
  const [loading, setLoading] = useState(true);
  const [typeFilters, setTypeFilters] = useState([]);
  const [sort, setSort] = useState('name');

  useEffect(() => {
    setLoading(true);
    fetchEntities()
      .then((res) => setEntities(Array.isArray(res.data) ? res.data : res.data?.entities || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() => {
    let list = entities;
    if (typeFilters.length) list = list.filter((e) => typeFilters.includes(e.type));
    list.sort((a, b) => {
      const nameA = entityLabel(a).toLowerCase();
      const nameB = entityLabel(b).toLowerCase();
      if (sort === 'name') return nameA.localeCompare(nameB);
      if (sort === 'type') return (a.type || '').localeCompare(b.type || '') || nameA.localeCompare(nameB);
      return 0;
    });
    return list;
  }, [entities, typeFilters, sort]);

  const counts = useMemo(() => {
    const c = {};
    entities.forEach((e) => { c[e.type] = (c[e.type] || 0) + 1; });
    return c;
  }, [entities]);

  return (
    <>
      <Breadcrumb items={[{ label: 'Entities', path: '/' }]} />
      <Layout>
        <TypeFilterSidebar selected={typeFilters} onChange={setTypeFilters} counts={counts} />
        <Main>
          <TopBar>
            <SearchBox />
            <ViewToggle>
              <ToggleBtn $active>List</ToggleBtn>
              <ToggleBtn onClick={() => navigate('/graph')}>Graph</ToggleBtn>
            </ViewToggle>
            <SortSelect value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
              <option value="name">Sort: Name</option>
              <option value="type">Sort: Type</option>
            </SortSelect>
            <Count>{filtered.length} entities</Count>
          </TopBar>
          {loading ? (
            <ShimmerGrid count={9} />
          ) : filtered.length === 0 ? (
            <Empty>No entities found. {typeFilters.length ? 'Try clearing filters.' : 'Check your API token.'}</Empty>
          ) : (
            <Grid>
              {filtered.map((e, i) => (
                <MiniEntityCard key={e.id} entity={e} index={i} />
              ))}
            </Grid>
          )}
        </Main>
      </Layout>
    </>
  );
}
