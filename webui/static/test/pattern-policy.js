// Selection rules for /test. Assignment and maximin optimization stay on the server.
export function patternFamily(pattern, library = []) {
  if (pattern === "plain") return "plain";
  if (pattern === "solid_black") return "solids";
  return library.find((item) => item.pattern === pattern)?.pattern_family || pattern;
}

export function patternChoiceBlock(group, pattern, groups, library, suggested) {
  if (!group) return "Select a map category first.";
  if (pattern === "04_waves_sine" && !group.is_water) {
    return "The sinusoidal wave is reserved for water areas.";
  }
  if (!suggested) return "";
  if (group.is_water && pattern !== "04_waves_sine") {
    return "Water areas use the sinusoidal wave with suggested patterns.";
  }
  if (!group.is_water && groups.some((item) => item.is_water)
      && patternFamily(pattern, library) === "waves") {
    return "The wave group is reserved for the map's water area.";
  }
  // The optimizer keeps existing plain areas fixed. A second plain choice
  // cannot be resolved by moving another category to a different family.
  if (pattern === "plain" && groups.some((item) => item.group_id !== group.group_id
      && item.pattern === "plain")) {
    return "The plain white group is already used on this map.";
  }
  return "";
}

export function patternChoiceWarnings(group, pattern, groups, library, suggested) {
  if (suggested || pattern === group?.pattern) return [];
  const warnings = [];
  const family = patternFamily(pattern, library);
  if (groups.some((item) => item.group_id !== group.group_id
      && patternFamily(item.pattern, library) === family)) {
    warnings.push("Minimal distinguishability! Are you sure you want to continue?");
  }
  if (group.is_water && pattern !== "04_waves_sine") {
    warnings.push("You are choosing a non-water pattern for a water area! Are you sure you want to continue?");
  }
  return warnings;
}
