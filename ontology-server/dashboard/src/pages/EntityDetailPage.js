import React from 'react';
import styled from 'styled-components';
import { useParams } from 'react-router-dom';
import EntityCard from '../components/EntityCard';
import RelationPanel from '../components/RelationPanel';
import Breadcrumb from '../components/Breadcrumb';
import { ShimmerBlock } from '../components/LoadingShimmer';
import { useEntity } from '../hooks/useEntities';
import { entityLabel } from '../utils/labels';

const Wrap = styled.div`
  max-width: 900px;
  margin: 0 auto;
  padding: 24px;
  width: 100%;
`;

const ErrorMsg = styled.div`
  padding: 40px;
  text-align: center;
  color: ${({ theme }) => theme.colors.danger};
`;

export default function EntityDetailPage() {
  const { id } = useParams();
  const { entity, relations, loading, error } = useEntity(id);

  const name = entity ? entityLabel(entity) : id;

  return (
    <>
      <Breadcrumb items={[
        { label: 'Entities', path: '/' },
        { label: name, path: `/entity/${id}` },
      ]} />
      <Wrap>
        {loading ? (
          <>
            <ShimmerBlock height="200px" />
            <ShimmerBlock height="300px" />
          </>
        ) : error ? (
          <ErrorMsg>Error loading entity: {error}</ErrorMsg>
        ) : entity ? (
          <>
            <EntityCard entity={entity} />
            <RelationPanel relations={relations} currentId={id} />
          </>
        ) : (
          <ErrorMsg>Entity not found</ErrorMsg>
        )}
      </Wrap>
    </>
  );
}
