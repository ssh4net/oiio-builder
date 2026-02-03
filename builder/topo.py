from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable


def topo_sort(nodes: list[str], deps: dict[str, list[str]]) -> list[str]:
    indegree = {n: 0 for n in nodes}
    graph: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dep in deps.get(node, []):
            if dep not in indegree:
                continue
            graph[dep].append(node)
            indegree[node] += 1

    queue = deque([n for n, d in indegree.items() if d == 0])
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt in graph.get(node, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(nodes):
        missing = [n for n in nodes if n not in order]
        raise RuntimeError(f"Dependency cycle or missing nodes: {missing}")
    return order
