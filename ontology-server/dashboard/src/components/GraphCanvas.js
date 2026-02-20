import React, { useRef, useEffect, useState, useCallback } from 'react';
import styled from 'styled-components';
import * as d3 from 'd3';
import { entityTypeColor } from '../styles/theme';
import { entityLabel } from '../utils/labels';

const Wrap = styled.div`
  flex: 1;
  position: relative;
  background: ${({ theme }) => theme.colors.bg};
  overflow: hidden;
`;

const Svg = styled.svg`
  width: 100%;
  height: 100%;
  display: block;
`;

const Controls = styled.div`
  position: absolute;
  bottom: 16px;
  right: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
`;

const CtrlBtn = styled.button`
  width: 36px; height: 36px;
  border-radius: ${({ theme }) => theme.radii.sm};
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1rem;
  transition: background ${({ theme }) => theme.transitions.fast};
  &:hover { background: ${({ theme }) => theme.colors.surfaceHover}; }
`;

const FilterBar = styled.div`
  position: absolute;
  top: 16px;
  left: 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
`;

const FilterChip = styled.button`
  font-size: 0.7rem;
  padding: 4px 10px;
  border-radius: 12px;
  border: 1px solid ${({ $active, $color }) => $active ? $color : '#21262D'};
  background: ${({ $active, $color }) => $active ? $color + '20' : 'transparent'};
  color: ${({ $active, $color, theme }) => $active ? $color : theme.colors.textMuted};
  transition: all 150ms;
  &:hover { border-color: ${({ $color }) => $color}; }
`;

export default function GraphCanvas({ entities = [], relations = [], onSelectEntity }) {
  const svgRef = useRef();
  const simRef = useRef();
  const [hoveredNode, setHoveredNode] = useState(null);

  // Derive relation types dynamically from the actual data
  const relTypes = React.useMemo(() => {
    const types = new Set();
    relations.forEach((r) => types.add(r.relation || r.type || 'related'));
    return Array.from(types).sort();
  }, [relations]);

  const [visibleRelTypes, setVisibleRelTypes] = useState(new Set());

  // Initialize visible types when relTypes change
  useEffect(() => {
    setVisibleRelTypes(new Set(relTypes));
  }, [relTypes]);

  const toggleRelType = (type) => {
    setVisibleRelTypes((prev) => {
      const next = new Set(prev);
      next.has(type) ? next.delete(type) : next.add(type);
      return next;
    });
  };

  const resetZoom = useCallback(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current);
    svg.transition().duration(500).call(
      d3.zoom().transform, d3.zoomIdentity
    );
  }, []);

  useEffect(() => {
    if (!svgRef.current || !entities.length) return;

    const svg = d3.select(svgRef.current);
    const { width, height } = svgRef.current.getBoundingClientRect();
    svg.selectAll('*').remove();

    // Build nodes & links
    const nodeMap = new Map();
    entities.forEach((e) => nodeMap.set(e.id, { ...e, relationCount: 0 }));

    const links = relations
      .filter((r) => visibleRelTypes.has(r.relation || r.type || 'related'))
      .filter((r) => nodeMap.has(r.source_id) && nodeMap.has(r.target_id))
      .map((r) => {
        nodeMap.get(r.source_id).relationCount++;
        nodeMap.get(r.target_id).relationCount++;
        return { source: r.source_id, target: r.target_id, type: r.relation || r.type || 'related' };
      });

    const nodes = Array.from(nodeMap.values());

    // Defs
    const defs = svg.append('defs');
    defs.append('marker')
      .attr('id', 'arrow')
      .attr('viewBox', '0 -5 10 10')
      .attr('refX', 20)
      .attr('refY', 0)
      .attr('markerWidth', 6)
      .attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-5L10,0L0,5')
      .attr('fill', '#484F58');

    // Add glow filter
    const filter = defs.append('filter').attr('id', 'glow');
    filter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
    filter.append('feMerge').selectAll('feMergeNode')
      .data(['blur', 'SourceGraphic']).enter()
      .append('feMergeNode').attr('in', (d) => d);

    const g = svg.append('g');

    // Zoom
    const zoom = d3.zoom()
      .scaleExtent([0.1, 5])
      .on('zoom', (e) => g.attr('transform', e.transform));
    svg.call(zoom);

    // Links
    const link = g.append('g').selectAll('line')
      .data(links).enter().append('line')
      .attr('stroke', '#21262D')
      .attr('stroke-width', 1)
      .attr('marker-end', 'url(#arrow)');

    // Link labels
    const linkLabel = g.append('g').selectAll('text')
      .data(links).enter().append('text')
      .text((d) => d.type)
      .attr('font-size', '8px')
      .attr('fill', '#484F58')
      .attr('text-anchor', 'middle')
      .attr('dy', -4);

    // Nodes
    const node = g.append('g').selectAll('circle')
      .data(nodes).enter().append('circle')
      .attr('r', (d) => Math.max(6, Math.min(20, 6 + d.relationCount * 2)))
      .attr('fill', (d) => entityTypeColor(d.type))
      .attr('stroke', 'none')
      .attr('cursor', 'pointer')
      .call(d3.drag()
        .on('start', (e, d) => { if (!e.active) simRef.current.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
        .on('end', (e, d) => { if (!e.active) simRef.current.alphaTarget(0); d.fx = null; d.fy = null; })
      );

    // Node labels
    const label = g.append('g').selectAll('text')
      .data(nodes).enter().append('text')
      .text((d) => entityLabel(d) || d.id.slice(0, 12))
      .attr('font-size', '10px')
      .attr('fill', '#E6EDF3')
      .attr('text-anchor', 'middle')
      .attr('dy', (d) => -Math.max(6, Math.min(20, 6 + d.relationCount * 2)) - 6);

    // Hover interactions
    node.on('mouseover', function (e, d) {
      setHoveredNode(d.id);
      const connected = new Set();
      links.forEach((l) => {
        const sId = typeof l.source === 'object' ? l.source.id : l.source;
        const tId = typeof l.target === 'object' ? l.target.id : l.target;
        if (sId === d.id) connected.add(tId);
        if (tId === d.id) connected.add(sId);
      });
      connected.add(d.id);
      node.attr('opacity', (n) => connected.has(n.id) ? 1 : 0.15);
      link.attr('opacity', (l) => {
        const sId = typeof l.source === 'object' ? l.source.id : l.source;
        const tId = typeof l.target === 'object' ? l.target.id : l.target;
        return sId === d.id || tId === d.id ? 1 : 0.05;
      });
      label.attr('opacity', (n) => connected.has(n.id) ? 1 : 0.15);
      d3.select(this).attr('filter', 'url(#glow)');
    })
    .on('mouseout', function () {
      setHoveredNode(null);
      node.attr('opacity', 1);
      link.attr('opacity', 1);
      label.attr('opacity', 1);
      d3.select(this).attr('filter', null);
    })
    .on('click', (e, d) => {
      if (onSelectEntity) onSelectEntity(d.id);
    });

    // Simulation
    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id((d) => d.id).distance(100))
      .force('charge', d3.forceManyBody().strength(-200))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide(25));

    simRef.current = sim;

    sim.on('tick', () => {
      link.attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y);
      linkLabel.attr('x', (d) => (d.source.x + d.target.x) / 2)
        .attr('y', (d) => (d.source.y + d.target.y) / 2);
      node.attr('cx', (d) => d.x).attr('cy', (d) => d.y);
      label.attr('x', (d) => d.x).attr('y', (d) => d.y);
    });

    return () => sim.stop();
  }, [entities, relations, visibleRelTypes, onSelectEntity]);

  return (
    <Wrap>
      <FilterBar>
        {relTypes.map((t) => (
          <FilterChip key={t} $active={visibleRelTypes.has(t)} $color="#6366F1" onClick={() => toggleRelType(t)}>
            {t}
          </FilterChip>
        ))}
      </FilterBar>
      <Svg ref={svgRef} />
      <Controls>
        <CtrlBtn onClick={resetZoom} title="Reset zoom">⟳</CtrlBtn>
      </Controls>
    </Wrap>
  );
}
