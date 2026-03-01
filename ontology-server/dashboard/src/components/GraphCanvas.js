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
  z-index: 2;
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

const InfoBar = styled.div`
  position: absolute;
  top: 16px;
  right: 60px;
  font-size: 0.75rem;
  color: ${({ theme }) => theme.colors.textMuted};
  background: ${({ theme }) => theme.colors.surface}CC;
  padding: 4px 10px;
  border-radius: ${({ theme }) => theme.radii.sm};
  pointer-events: none;
`;

const LoadingOverlay = styled.div`
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: ${({ theme }) => theme.colors.textMuted};
  font-size: 0.9rem;
  z-index: 3;
  pointer-events: none;
`;

export default function GraphCanvas({
  entities = [],
  relations = [],
  expandedNodes = new Set(),
  totalRelCounts = {},
  onExpandNode,
  onCollapseNode,
  onSelectEntity,
  showOrphans = false,
  onToggleOrphans,
  loading = false,
}) {
  const svgRef = useRef();
  const simRef = useRef();
  const zoomRef = useRef();

  // Stable refs for D3 event handlers (avoid stale closures)
  const expandedRef = useRef(expandedNodes);
  const showOrphansRef = useRef(showOrphans);
  const onExpandRef = useRef(onExpandNode);
  const onCollapseRef = useRef(onCollapseNode);
  const onSelectRef = useRef(onSelectEntity);

  useEffect(() => { expandedRef.current = expandedNodes; }, [expandedNodes]);
  useEffect(() => { showOrphansRef.current = showOrphans; }, [showOrphans]);
  useEffect(() => { onExpandRef.current = onExpandNode; }, [onExpandNode]);
  useEffect(() => { onCollapseRef.current = onCollapseNode; }, [onCollapseNode]);
  useEffect(() => { onSelectRef.current = onSelectEntity; }, [onSelectEntity]);

  // Derive relation types from data
  const relTypes = React.useMemo(() => {
    const types = new Set();
    relations.forEach((r) => types.add(r.relation || r.type || 'related'));
    return Array.from(types).sort();
  }, [relations]);

  const [visibleRelTypes, setVisibleRelTypes] = useState(new Set());

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
    if (!svgRef.current || !zoomRef.current) return;
    const svg = d3.select(svgRef.current);
    svg.transition().duration(500).call(zoomRef.current.transform, d3.zoomIdentity);
  }, []);

  // Main D3 render
  useEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current);
    const { width, height } = svgRef.current.getBoundingClientRect();
    svg.selectAll('*').remove();
    if (!entities.length) return;

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
      .attr('refX', 20).attr('refY', 0)
      .attr('markerWidth', 6).attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', '#484F58');

    const filter = defs.append('filter').attr('id', 'glow');
    filter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
    filter.append('feMerge').selectAll('feMergeNode')
      .data(['blur', 'SourceGraphic']).enter()
      .append('feMergeNode').attr('in', (d) => d);

    const g = svg.append('g');

    // Zoom (disable dblclick zoom — we use it for detail panel)
    const zoom = d3.zoom()
      .scaleExtent([0.1, 5])
      .on('zoom', (e) => g.attr('transform', e.transform));
    svg.call(zoom).on('dblclick.zoom', null);
    zoomRef.current = zoom;

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

    // Node radius helper
    const getRadius = (d) => {
      if (d._isGroup) return 24;
      if (d.type === 'Person') return Math.max(14, Math.min(26, 14 + d.relationCount));
      return Math.max(6, Math.min(18, 6 + d.relationCount * 2));
    };

    // Node groups (g element per node for circle + badge/count)
    const nodeGroup = g.append('g').selectAll('g')
      .data(nodes).enter().append('g')
      .attr('cursor', 'pointer')
      .call(d3.drag()
        .on('start', (e, d) => {
          if (!e.active) simRef.current.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
        .on('end', (e, d) => {
          if (!e.active) simRef.current.alphaTarget(0);
          d.fx = null; d.fy = null;
        })
      );

    // Main circles
    nodeGroup.append('circle')
      .attr('class', 'node-circle')
      .attr('r', getRadius)
      .attr('fill', (d) => {
        if (d._isGroup) return entityTypeColor(d.type) + '30';
        return entityTypeColor(d.type);
      })
      .attr('stroke', (d) => {
        if (d._isGroup) return entityTypeColor(d.type);
        if (expandedNodes.has(d.id)) return entityTypeColor(d.type);
        return 'none';
      })
      .attr('stroke-width', (d) => (d._isGroup || expandedNodes.has(d.id)) ? 2.5 : 0)
      .attr('stroke-dasharray', (d) => d._isGroup ? '5 3' : null)
      .attr('filter', (d) => expandedNodes.has(d.id) ? 'url(#glow)' : null);

    // Count text inside group nodes
    nodeGroup.filter((d) => d._isGroup).append('text')
      .attr('class', 'group-count')
      .attr('text-anchor', 'middle')
      .attr('dy', 5)
      .attr('font-size', '12px')
      .attr('font-weight', 'bold')
      .attr('fill', '#E6EDF3')
      .attr('pointer-events', 'none')
      .text((d) => d._groupCount);

    // Badge for unexpanded Person nodes (show total relation count)
    const badgeNodes = nodeGroup.filter(
      (d) => d.type === 'Person' && !d._isGroup && !expandedNodes.has(d.id) && (totalRelCounts[d.id] || 0) > 0
    );
    badgeNodes.append('circle')
      .attr('cx', (d) => getRadius(d) - 2)
      .attr('cy', (d) => -getRadius(d) + 2)
      .attr('r', 9)
      .attr('fill', '#21262D')
      .attr('stroke', '#484F58')
      .attr('stroke-width', 1);
    badgeNodes.append('text')
      .attr('x', (d) => getRadius(d) - 2)
      .attr('y', (d) => -getRadius(d) + 2)
      .attr('text-anchor', 'middle')
      .attr('dy', 3)
      .attr('font-size', '8px')
      .attr('fill', '#E6EDF3')
      .attr('pointer-events', 'none')
      .text((d) => totalRelCounts[d.id] || 0);

    // Node labels
    const label = g.append('g').selectAll('text')
      .data(nodes).enter().append('text')
      .text((d) => entityLabel(d) || d.id.slice(0, 12))
      .attr('font-size', '10px')
      .attr('fill', '#E6EDF3')
      .attr('text-anchor', 'middle')
      .attr('pointer-events', 'none');

    // Click: single = expand/collapse, double = detail panel
    let clickTimer = null;
    nodeGroup.on('click', function (event, d) {
      event.stopPropagation();
      if (clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
        // Double click → detail panel (skip for group nodes)
        if (!d._isGroup && onSelectRef.current) onSelectRef.current(d.id);
        return;
      }
      clickTimer = setTimeout(() => {
        clickTimer = null;
        if (showOrphansRef.current) return;

        if (d._isGroup) {
          // Group node → expand (replace with individual entities)
          if (onExpandRef.current) onExpandRef.current(d.id);
        } else if (expandedRef.current.has(d.id)) {
          // Already expanded → collapse
          if (onCollapseRef.current) onCollapseRef.current(d.id);
        } else {
          // Not expanded → expand
          if (onExpandRef.current) onExpandRef.current(d.id);
        }
      }, 250);
    });

    // Hover interactions
    nodeGroup.on('mouseover', function (e, d) {
      const connected = new Set([d.id]);
      links.forEach((l) => {
        const sId = typeof l.source === 'object' ? l.source.id : l.source;
        const tId = typeof l.target === 'object' ? l.target.id : l.target;
        if (sId === d.id) connected.add(tId);
        if (tId === d.id) connected.add(sId);
      });
      nodeGroup.attr('opacity', (n) => connected.has(n.id) ? 1 : 0.15);
      link.attr('opacity', (l) => {
        const sId = typeof l.source === 'object' ? l.source.id : l.source;
        const tId = typeof l.target === 'object' ? l.target.id : l.target;
        return sId === d.id || tId === d.id ? 1 : 0.05;
      });
      linkLabel.attr('opacity', (l) => {
        const sId = typeof l.source === 'object' ? l.source.id : l.source;
        const tId = typeof l.target === 'object' ? l.target.id : l.target;
        return sId === d.id || tId === d.id ? 1 : 0.05;
      });
      label.attr('opacity', (n) => connected.has(n.id) ? 1 : 0.15);
      d3.select(this).select('.node-circle').attr('filter', 'url(#glow)');
    })
    .on('mouseout', function () {
      nodeGroup.attr('opacity', 1);
      link.attr('opacity', 1);
      linkLabel.attr('opacity', 1);
      label.attr('opacity', 1);
      d3.select(this).select('.node-circle')
        .attr('filter', (d) => expandedRef.current.has(d.id) ? 'url(#glow)' : null);
    });

    // Simulation
    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id((d) => d.id).distance(120))
      .force('charge', d3.forceManyBody().strength(-250))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide(35));

    simRef.current = sim;

    sim.on('tick', () => {
      link.attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y);
      linkLabel.attr('x', (d) => (d.source.x + d.target.x) / 2)
        .attr('y', (d) => (d.source.y + d.target.y) / 2);
      nodeGroup.attr('transform', (d) => `translate(${d.x},${d.y})`);
      label.attr('x', (d) => d.x).attr('y', (d) => d.y - getRadius(d) - 6);
    });

    return () => sim.stop();
  }, [entities, relations, visibleRelTypes, expandedNodes, totalRelCounts]);

  return (
    <Wrap>
      {!showOrphans && relTypes.length > 0 && (
        <FilterBar>
          {relTypes.map((t) => (
            <FilterChip key={t} $active={visibleRelTypes.has(t)} $color="#6366F1" onClick={() => toggleRelType(t)}>
              {t}
            </FilterChip>
          ))}
        </FilterBar>
      )}
      <InfoBar>
        {entities.length} {showOrphans ? 'orphans' : 'nodes'}
      </InfoBar>
      {loading && <LoadingOverlay>Loading...</LoadingOverlay>}
      <Svg ref={svgRef} />
      <Controls>
        <CtrlBtn onClick={resetZoom} title="Reset zoom">&#x27F3;</CtrlBtn>
        {onToggleOrphans && (
          <CtrlBtn
            onClick={onToggleOrphans}
            title={showOrphans ? 'Back to level view' : 'Show orphan entities'}
            style={showOrphans ? { background: '#6366F1', color: '#fff', borderColor: '#6366F1' } : {}}
          >
            &#x00D8;
          </CtrlBtn>
        )}
      </Controls>
    </Wrap>
  );
}
