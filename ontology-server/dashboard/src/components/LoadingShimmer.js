import React from 'react';
import styled from 'styled-components';

const ShimmerCard = styled.div`
  height: ${({ $h }) => $h || '120px'};
  border-radius: ${({ theme }) => theme.radii.md};
  animation-delay: ${({ $delay }) => $delay || '0ms'};
`;

const Grid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  padding: 20px;
`;

export function ShimmerGrid({ count = 6 }) {
  return (
    <Grid>
      {Array.from({ length: count }).map((_, i) => (
        <ShimmerCard key={i} className="shimmer" $delay={`${i * 80}ms`} />
      ))}
    </Grid>
  );
}

export function ShimmerBlock({ height = '200px' }) {
  return <ShimmerCard className="shimmer" $h={height} style={{ margin: 20 }} />;
}
