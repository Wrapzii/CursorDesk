(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.CursorDeskSubagentParser = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();

  function classify(action, overallLoading, isLastNotification) {
    const verb = clean(action);
    if (overallLoading && isLastNotification) return "running";
    if (/fail|error|cancel|stop/i.test(verb)) return "error";
    if (/finish|complete|done/i.test(verb)) return "completed";
    return "running";
  }

  function parseGroups(descriptors, overallLoading) {
    const source = Array.isArray(descriptors) ? descriptors.filter(Boolean) : [];
    // Virtualized transcripts only prove what is current at the live tail. The
    // caller supplies descriptors in DOM order, so the newest group wins.
    const latest = source.length ? [source[source.length - 1]] : [];
    const groups = latest.map((descriptor, groupIndex) => {
      const action = clean(descriptor.action);
      const details = clean(descriptor.details);
      const parsedCount = Number(descriptor.count)
        || Number((details.match(/\b(\d+)\s+sub\s*agents?\b/i) || [])[1])
        || 0;
      const state = classify(
        action,
        !!overallLoading,
        !!descriptor.isLastNotification
      );
      const agents = (Array.isArray(descriptor.agents) ? descriptor.agents : [])
        .filter(Boolean)
        .map((agent, index) => ({
          id: clean(agent.id) || `subagent:${groupIndex}:${index}`,
          index: Number(agent.index) || index + 1,
          label: clean(agent.label) || `Agent ${index + 1}`,
          title: clean(agent.title),
          status: clean(agent.status),
          state: ["running", "completed", "error"].includes(agent.state)
            ? agent.state
            : state,
          model: clean(agent.model),
          running: agent.state === "running",
        }));
      const detailed = agents.length > 0;
      const count = parsedCount || agents.length;
      const counts = detailed
        ? {
            running: agents.filter((agent) => agent.state === "running").length,
            completed: agents.filter((agent) => agent.state === "completed").length,
            error: agents.filter((agent) => agent.state === "error").length,
          }
        : {
            running: state === "running" ? count : 0,
            completed: state === "completed" ? count : 0,
            error: state === "error" ? count : 0,
          };
      return {
        id: clean(descriptor.id) || `subagent-group:${groupIndex}`,
        action,
        details,
        count,
        state,
        expanded: !!descriptor.expanded,
        detailed,
        agents,
        ...counts,
      };
    });
    const agents = groups.flatMap((group) => group.agents);
    return {
      groups,
      agents,
      total: groups.reduce((sum, group) => sum + group.count, 0),
      running: groups.reduce((sum, group) => sum + group.running, 0),
      completed: groups.reduce((sum, group) => sum + group.completed, 0),
      error: groups.reduce((sum, group) => sum + group.error, 0),
    };
  }

  return { classify, parseGroups };
});
