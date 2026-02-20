from __future__ import annotations

from collections import defaultdict, deque
import heapq
from typing import Iterable


def topo_sort(nodes: list[str], deps: dict[str, list[str]], preferred_order: list[str] | None = None) -> list[str]:
    indegree = {n: 0 for n in nodes}
    graph: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dep in deps.get(node, []):
            if dep not in indegree:
                continue
            graph[dep].append(node)
            indegree[node] += 1

    if not preferred_order:
        queue = deque([n for n, d in indegree.items() if d == 0])
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for nxt in graph.get(node, []):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
    else:
        preferred_index = {name: idx for idx, name in enumerate(preferred_order)}
        original_index = {name: idx for idx, name in enumerate(nodes)}

        def _key(name: str) -> tuple[int, int]:
            return preferred_index.get(name, len(preferred_index) + 1), original_index[name]

        heap: list[tuple[tuple[int, int], str]] = [(_key(n), n) for n, d in indegree.items() if d == 0]
        heapq.heapify(heap)
        order = []
        while heap:
            _k, node = heapq.heappop(heap)
            order.append(node)
            for nxt in graph.get(node, []):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    heapq.heappush(heap, (_key(nxt), nxt))

    if len(order) != len(nodes):
        missing = [n for n in nodes if n not in order]
        raise RuntimeError(f"Dependency cycle or missing nodes: {missing}")
    return order
