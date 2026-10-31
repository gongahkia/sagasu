/* Approximate map coordinates from OpenStreetMap/Wikidata; geometry is illustrative. */
(function (root) {
  const buildings = [
    { id: 'LKCSB', name: 'Lee Kong Chian School of Business', prefix: 'LKCSB GSR', lat: 1.29528, lon: 103.85055 },
    { id: 'SOA', name: 'School of Accountancy', prefix: 'SOA GSR', lat: 1.29591, lon: 103.84959 },
    { id: 'SCIS1', name: 'School of Computing and Information Systems 1', prefix: 'SCIS1 GSR', lat: 1.29761, lon: 103.84953 },
    { id: 'SOE/SCIS2', name: 'School of Economics / SCIS 2', prefix: 'SOE/SCIS2 GSR', lat: 1.29788, lon: 103.84891 },
  ];
  // Local projection: east is +x, north is -z; 1 unit is about 37 metres.
  function project(lat, lon) {
    return { x: (lon - 103.8497) * 3000, z: -(lat - 1.2966) * 3000 };
  }
  const floorPlans = {
  "SOA:2": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEi2XKbf95Y2MP-yOCQOpzwVMKmhq8f57eMwtG7XqZe3zMoQoqb5Q5oEes9dnMQrDOJNnNdyRD0UVx23y46QY-2atcxVC2fzxrzhM50DfzMOVAGhcY8Bs1qPv9SLRRBFx_dUgLqZGJTX3bYX/s1600/School+of+Accountancy+Level+2+-.JPG",
  "SOA:3": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEhmvtZaJB4ZzpoHH1dOuf7hloy6v9Vsd4jBoVEXwgYuTX5o_RAb8nMeszeQBSYM9uXfmzNPQhN5fZeP_2ng2LWol4dPBNihNfRBu4RZIXUPXIQQWGC7Jo9S0XzyT14_RNvsbggVaemjaXM3/s1600/School+of+Accountancy+Level+3+-.JPG",
  "LKCSB:2": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjVu5Q-s5X2AReJmTk2vbaPKv766PH3D7_fIzfeekW28wzq4Xf3I-xU4p6ZQUoY3GUh0MMOK4AOvvNW-_8wJPp6zK4BmQPHJAA3iDxnhiEh1HLl_0n1Wf2tzEevkCt3ZHLWZ-jncs0vP_EE/s1600/School+of+Business+Level+2+-.JPG",
  "LKCSB:3": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEiBwjlAt95nFcpvtd3OQfBowxRaklRUTJwunM04gs-8yyRUqgiMjaEKI2Y0_Adp22WBvRjmQXs8NT3pSWOZeMzsqQ7gaeHLLBya2D35TQKDLSTT5C-Fn1PlTZ25MTC2INV-48gp5U0To0J-/s1600/School+of+Business+Level+3+-.JPG",
  "SOE/SCIS2:2": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEhNgw6B41U7Qr1okXh0XQjFtNDLVds8u_wfZ_h6d8iMmFXJrOnedzYWlzRd3XTXO3I06RIsULniIAmusKghqluMEAWJTi8yHlhy_jKeHT_3xN_tkQMcPGUOB4cYnFqGujcrxIPY7oDE8mlV/s1600/School+of+Economics+Level+2+-.JPG",
  "SOE/SCIS2:3": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEhAU4ZomLIk1fbrPGXfbBP3orbnddYeUWiF0gJur63_w2YQLWmnOCAKZTX_XXxyo7f3REEGxr_e0ENAvDwi86pxxbTU2J43u5u2GM5k_0JetSbad9K2NaGAnOnJ2Nfs3EWzGDIHSvJ432nR/s1600/School+of+Economics+Level+3+-.JPG",
  "SOE/SCIS2:4": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgnkPlD7LdvlU0boSg-YroxRj-Qd1xATj4e75dByNPFGpe4oTHSXGTfm2FMBTZlzO24uA5Tqpf1HijbJMIFupmWjuZvEYs70W7HgZteBB5nb0OWsJ0xJgyJcsZVfoLzPX3BOkhIEGGz9vuJ/s1600/School+of+Economics+Level+4+-.JPG",
  "SOE/SCIS2:5": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjPJCc5EiUdSIvgRob5BPmtjgMwlxg_TVpc271DaIqha72e_rVAkgOxFLnTXIAU_54XZmaahkqSy4q6xIlADp24PDeatlON6mZSThR2RKb1URjOQqjAJj5fae1BbnjXmJdr7uAwy5IO-dCc/s1600/School+of+Economics+Level+5+-.JPG",
  "SCIS1:2": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjBRaOjqtBbY9sbe5LMmIeKTeZXjwsBwrLTJ-eDU1oByrpwhMu7iI6hM-afZZJzVmpZ78ybU0IAFU7xSVC0Q378X-jfIXEZ99ICThZZ7gX0WvbJH55wsXfuwZHC8qHfHpxPCcMncQzDHaR2/s1600/School+of+Information+Systems+Level+2+-.JPG",
  "SCIS1:3": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjxhDnBpI4HW-nSm6o90GxcOm9zqH4cc2HBxqg2Q4C3gM-BtM5hLXe5sYgWf0jogmwel9dvzTleE7PIz3j4IaJ2nQrXJx-QxS7iUsCj-dxuk0z5s3dlGDR2LB8GIg9UGEJWtP7kChuF7MaX/s1600/School+of+Information+Systems+Level+3+-.JPG"
};
  for (const floor of [2,3,4,5]) floorPlans[`LKS:${floor}`] = `https://white-smoke-0f711b810.1.azurestaticapps.net/images/lks${floor}.gif`;
  function buildingFor(name) {
    const normalized = name.trim().toUpperCase();
    return buildings.find(building => normalized.startsWith(building.prefix + ' ') || normalized === building.prefix) || null;
  }
  function counts(rooms) {
    const result = Object.fromEntries(buildings.map(building => [building.id, 0]));
    let unmapped = 0;
    for (const room of rooms) {
      const building = buildingFor(room.name);
      if (building) result[building.id]++; else unmapped++;
    }
    return { buildings: result, unmapped };
  }
  function floorFor(name) {
    const building = buildingFor(name);
    const match = name.match(/\bGSR\s+(\d+)-/i);
    return building && match ? `${building.id}:${Number(match[1])}` : null;
  }
  const api = { buildings, buildingFor, counts, project, floorPlans, floorFor };
  root.SagasuMapData = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof globalThis === 'undefined' ? this : globalThis);
