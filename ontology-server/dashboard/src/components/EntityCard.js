import React from 'react';
import styled from 'styled-components';
import { entityTypeColor } from '../styles/theme';
import { entityLabel } from '../utils/labels';

const Card = styled.div`
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  border-radius: ${({ theme }) => theme.radii.lg};
  overflow: hidden;
  animation: fadeIn 300ms ease;
`;

const Header = styled.div`
  padding: 24px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  display: flex;
  align-items: center;
  gap: 16px;
`;

const TypeIcon = styled.div`
  width: 48px; height: 48px;
  border-radius: ${({ theme }) => theme.radii.md};
  background: ${({ $color }) => $color + '20'};
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.5rem;
  color: ${({ $color }) => $color};
  flex-shrink: 0;
`;

const HeaderInfo = styled.div`flex: 1;`;
const Title = styled.h1`font-size: 1.4rem; font-weight: 700; margin-bottom: 4px;`;
const Subtitle = styled.div`
  font-size: 0.8rem;
  color: ${({ theme }) => theme.colors.textSecondary};
  font-family: ${({ theme }) => theme.fonts.mono};
`;

const Body = styled.div`padding: 24px;`;

const iconMap = {
  Person: '👤', Account: '🔑', Business: '🏢', Location: '📍',
  Task: '✅', Event: '📅', Project: '📁', Interest: '💡', Tag: '🏷️',
};

export default function EntityCard({ entity }) {
  if (!entity) return null;
  const color = entityTypeColor(entity.type);
  const name = entityLabel(entity);

  return (
    <Card>
      <Header>
        <TypeIcon $color={color}>{iconMap[entity.type] || '⬡'}</TypeIcon>
        <HeaderInfo>
          <Title>{name}</Title>
          <Subtitle>{entity.type} · {entity.id}</Subtitle>
        </HeaderInfo>
      </Header>
      <Body>
        <PropertyGrid properties={entity.properties} />
      </Body>
    </Card>
  );
}

const Grid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
`;

const PropItem = styled.div`
  padding: 12px;
  background: ${({ theme }) => theme.colors.bg};
  border-radius: ${({ theme }) => theme.radii.sm};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
`;

const PropKey = styled.div`
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: ${({ theme }) => theme.colors.textMuted};
  margin-bottom: 4px;
`;

const PropVal = styled.div`
  font-size: 0.9rem;
  word-break: break-all;
  color: ${({ theme }) => theme.colors.text};
`;

function PropertyGrid({ properties }) {
  if (!properties || Object.keys(properties).length === 0) {
    return <div style={{ color: '#8B949E', fontSize: '0.85rem' }}>No properties</div>;
  }
  return (
    <Grid>
      {Object.entries(properties).map(([k, v]) => (
        <PropItem key={k}>
          <PropKey>{k.replace(/_/g, ' ')}</PropKey>
          <PropVal>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</PropVal>
        </PropItem>
      ))}
    </Grid>
  );
}
