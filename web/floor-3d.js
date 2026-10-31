const host = document.getElementById('floor-3d');
const message = document.getElementById('floor-3d-message');
try {
  const THREE = await import('https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js');
  const { OrbitControls } = await import('https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/controls/OrbitControls.js');
  const geometry = window.SagasuFloorGeometry;
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0xe8efe4);
  const camera = new THREE.PerspectiveCamera(38, 1, .1, 500);
  const renderer = new THREE.WebGLRenderer({antialias:true}); renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  host.append(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff,0x839e8a,2.2));
  const light=new THREE.DirectionalLight(0xffffff,1.5); light.position.set(7,18,12);scene.add(light);
  const world=new THREE.Group();scene.add(world);
  let stack=world, animation=0;
  const controls=new OrbitControls(camera,renderer.domElement); controls.enableDamping=false; controls.minDistance=5;controls.maxDistance=350;controls.maxPolarAngle=Math.PI/2.05;controls.addEventListener('change',render);
  camera.position.set(9,14,19);controls.target.set(0,2,0);controls.update();
  function fitCamera(){
    const bounds = new THREE.Box3();
    for(const child of world.children) if(!child.userData.ground) bounds.union(new THREE.Box3().setFromObject(child));
    if (bounds.isEmpty()) return;
    const size = bounds.getSize(new THREE.Vector3());
    const center = bounds.getCenter(new THREE.Vector3());
    const extent = Math.max(size.z * .85 + size.y * .5, size.x / Math.max(.55, camera.aspect), 5);
    const distance = (extent / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.38;
    controls.target.copy(center);
    camera.position.set(center.x + distance * .22, center.y + distance * .68, center.z + distance * .72);
    controls.update();
  }
  function render(){renderer.render(scene,camera);}
  function resize(){const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;camera.aspect=w/h;camera.updateProjectionMatrix();renderer.setSize(w,h);if(lastFocus&&!animation)fitCamera();render();}
  function label(text,opacity){const c=document.createElement('canvas');c.width=256;c.height=128;const x=c.getContext('2d');x.fillStyle='#fff';x.fillRect(0,0,256,128);x.fillStyle='#20352e';x.font='bold 44px system-ui';x.textAlign='center';x.fillText(text,128,82);const texture=new THREE.CanvasTexture(c);const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,transparent:true,opacity,depthTest:false}));sprite.scale.set(.75,.38,1);return sprite;}
  const outlines={
    LKCSB:[[290,310],[1200,310],[1220,440],[1180,550],[1230,700],[1300,950],[610,1070],[580,890],[700,860],[670,660],[285,650]],
    SOA:[[350,440],[410,390],[1110,390],[1240,440],[1250,900],[1090,1010],[950,925],[950,810],[860,810],[860,880],[620,730],[550,660],[430,690],[350,600]],
    SCIS1:[[365,275],[1260,275],[1310,415],[1190,580],[800,580],[800,700],[940,760],[850,1000],[650,1100],[430,1010],[365,870],[390,575]],
    'SOE/SCIS2':[[320,420],[1230,420],[1220,550],[1320,760],[1200,880],[950,940],[600,940],[400,900],[350,730],[410,650]],
    LKS:geometry.libraryOutline,
  };
  const atriums={'LKCSB:2':[850,605,990,760],'LKCSB:3':[900,610,1035,770],'SOE/SCIS2:2':[628,628,890,725],'SOE/SCIS2:3':[623,584,918,680],'SOE/SCIS2:4':[642,630,943,728],'SOE/SCIS2:5':[658,657,955,750]};
  function point(x,y){return [(x-800)/100,-(y-600)/100];}
  function material(color,opacity){return new THREE.MeshStandardMaterial({color,transparent:opacity<1,opacity,depthWrite:opacity===1,side:THREE.DoubleSide});}
  function floorSurface(key,layer,opacity){
    const [building]=key.split(':'); const outline=outlines[building];if(!outline)return;
    const shape=new THREE.Shape();outline.forEach(([x,y],i)=>{const p=point(x,y);if(i)shape.lineTo(...p);else shape.moveTo(...p);});shape.closePath();
    const hole=atriums[key];if(hole){const [x1,y1,x2,y2]=hole;const h=new THREE.Path();[[x1,y1],[x1,y2],[x2,y2],[x2,y1]].forEach(([x,y],i)=>{const p=point(x,y);if(i)h.lineTo(...p);else h.moveTo(...p);});h.closePath();shape.holes.push(h);}
    const mesh=new THREE.Mesh(new THREE.ExtrudeGeometry(shape,{depth:.12,bevelEnabled:false}),material(0xe7eee2,opacity));mesh.rotation.x=-Math.PI/2;layer.add(mesh);
  }
  function room(layer,rect,color,opacity,text,showLabel){
    const [x,y,w,h]=rect;const height=.55;
    const mesh=new THREE.Mesh(new THREE.BoxGeometry(w/100,height,h/100),material(color,opacity));mesh.position.set((x-800)/100,height/2+.12,(y-600)/100);layer.add(mesh);
    const edges=new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry),new THREE.LineBasicMaterial({color:0x446657,transparent:opacity<1,opacity}));edges.position.copy(mesh.position);layer.add(edges);
    if(showLabel){const sprite=label(text,opacity);sprite.position.set(mesh.position.x,height+.5,mesh.position.z);layer.add(sprite);}
  }
  // Facade massing interprets photo-visible glass, columns and horizontal bands.
  // Heights, unsurveyed sides and context are intentionally approximate.
  const exteriorLevels = {LKCSB:5, SOA:5, SCIS1:5, 'SOE/SCIS2':6, LKS:6};
  function exterior(building){
    const outline=outlines[building]; if(!outline)return;
    const levels=exteriorLevels[building]; const corners=outline.map(([x,y])=>[(x-800)/100,(y-600)/100]);
    const glazing=material(0x79a79b,.07), lines=[];
    const glass=new THREE.Group();stack.add(glass);
    for(let level=1;level<=levels;level++){
      const lower=(level-2)*1.55, upper=lower+1.55;
      for(let i=0;i<corners.length;i++){
        const [x1,z1]=corners[i], [x2,z2]=corners[(i+1)%corners.length];
        const dx=x2-x1,dz=z2-z1,length=Math.hypot(dx,dz);if(length<.01)continue;
        const panel=new THREE.Mesh(new THREE.BoxGeometry(length,1.41,.035),glazing);
        panel.position.set((x1+x2)/2,lower+.77,(z1+z2)/2);panel.rotation.y=-Math.atan2(dz,dx);glass.add(panel);
        lines.push(x1,lower,z1,x2,lower,z2);
        const segments=Math.max(1,Math.ceil(length/.75));
        for(let j=0;j<segments;j++){const t=j/segments;const x=x1+dx*t,z=z1+dz*t;lines.push(x,lower,z,x,upper,z);}
        if(level===levels)lines.push(x1,upper,z1,x2,upper,z2);
      }
    }
    const frame=new THREE.BufferGeometry();frame.setAttribute('position',new THREE.Float32BufferAttribute(lines,3));
    stack.add(new THREE.LineSegments(frame,new THREE.LineBasicMaterial({color:0x55756b,transparent:true,opacity:.22,depthWrite:false})));
    // Interpreted exterior details: references show curved cores, a projecting
    // roof, glazed upper floors, and columns. Sizes and unseen sides are estimates.
    const roofShape = new THREE.Shape();
    corners.forEach(([x,z],i)=>{if(i)roofShape.lineTo(x,-z);else roofShape.moveTo(x,-z);});
    roofShape.closePath();
    const roof = new THREE.Mesh(new THREE.ShapeGeometry(roofShape), material(0xbaccc2,.12));
    roof.rotation.x=-Math.PI/2;roof.position.y=(levels-1)*1.55+.04;stack.add(roof);
    if(building==='LKCSB' || building==='SCIS1'){
      const corePositions = building==='LKCSB' ? [[-5,-1.8],[4.1,-1.6]] : [[-4,-.7],[.1,3.5]];
      for(const [x,z] of corePositions){
        const core=new THREE.Mesh(new THREE.CylinderGeometry(.48,.48,levels*1.55,24),material(0x8da49a,.2));
        core.position.set(x,(levels-2)*1.55/2,z);stack.add(core);
      }
      for(const x of [-3,-1.5,0,1.5,3]){
        const column=new THREE.Mesh(new THREE.CylinderGeometry(.08,.08,2.9,10),material(0xe7eee8,.65));
        column.position.set(x,-.10,3.45);stack.add(column);
      }
    }
    if(building==='LKS'){
      // SMU describes greenery and horizontal louvres on the library facade.
      const green=[];
      for(let level=1;level<=levels;level++)for(let i=0;i<corners.length;i+=2){
        const [x1,z1]=corners[i],[x2,z2]=corners[(i+1)%corners.length];green.push(x1,(level-1)*1.55-.12,z1,x2,(level-1)*1.55-.12,z2);
      }
      const bands=new THREE.BufferGeometry();bands.setAttribute('position',new THREE.Float32BufferAttribute(green,3));
      stack.add(new THREE.LineSegments(bands,new THREE.LineBasicMaterial({color:0x51805b,transparent:true,opacity:.8,depthWrite:false})));
    }
  }
  function clear(){while(world.children.length){const item=world.children[0];world.remove(item);item.traverse(obj=>{if(obj.geometry)obj.geometry.dispose();if(obj.material){if(obj.material.map)obj.material.map.dispose();obj.material.dispose();}});}}
  function focusRoom(target){
    cancelAnimationFrame(animation);
    const fromPosition=camera.position.clone(), fromTarget=controls.target.clone();
    const toTarget=target.clone(), toPosition=target.clone().add(new THREE.Vector3(3.6,5.5,6.2));
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const duration=reduced?0:1800, started=performance.now();
    function step(now){const t=duration?Math.min(1,(now-started)/duration):1;const eased=1-Math.pow(1-t,3);camera.position.lerpVectors(fromPosition,toPosition,eased);controls.target.lerpVectors(fromTarget,toTarget,eased);controls.update();render();if(t<1)animation=requestAnimationFrame(step);else animation=0;}
    animation=requestAnimationFrame(step);
  }
  let lastFocus='';
  function update(event){
    const {key,rooms,selectedRoom,focusRequest,showOtherFloors,showExterior}=event.detail;clear();
    if(!key){message.hidden=false;message.textContent='Choose a building and floor to view its model.';render();return;}
    const [selectedBuilding,selected]=key.split(':');const selectedFloor=Number(selected);const free=new Set(rooms.map(r=>r.name.toUpperCase()));
    const locations=[...window.SagasuMapData.buildings,{id:'LKS',lat:1.2969,lon:103.8498}];
    let focusedCount=0, target=null;
    const ground=new THREE.Mesh(new THREE.PlaneGeometry(160,160),new THREE.MeshStandardMaterial({color:0xdce7d8,side:THREE.DoubleSide}));ground.userData.ground=true;ground.rotation.x=-Math.PI/2;ground.position.y=-1.7;world.add(ground);
    for(const buildingData of locations){
      const building=buildingData.id, selectedModel=building===selectedBuilding;
      const projected=window.SagasuMapData.project(buildingData.lat,buildingData.lon);
      // The floor plans use image-derived units, not surveyed dimensions. Spread
      // their projected centres to avoid suggesting overlapping buildings.
      const offset=new THREE.Vector3(projected.x*11,0,projected.z*11);
      stack=new THREE.Group();stack.position.copy(offset);world.add(stack);
      const source=building==='LKS'?geometry.library:geometry.positions;
      const keys=Object.keys(source).filter(k=>k.split(':')[0]===building).sort((a,b)=>Number(a.split(':')[1])-Number(b.split(':')[1]));
      for(const floorKey of keys){
        const floor=Number(floorKey.split(':')[1]);const focused=selectedModel&&floor===selectedFloor;
        if(selectedModel&&!focused&&!showOtherFloors)continue;
        const opacity=focused?1:.12;const layer=new THREE.Group();layer.position.y=(floor-2)*1.55;stack.add(layer);floorSurface(floorKey,layer,opacity);
        if(!selectedModel)continue;
        if(building==='LKS'){
          const model=geometry.library[floorKey];
          for(const [x,y,w,h] of model.amenities)room(layer,[x,y,w,h],0xb5c2b7,opacity,'',false);
          for(const [number,x,y,w,h] of model.rooms){room(layer,[x,y,w,h],0x8fa79a,opacity,`${floor}-${number}`,focused);if(focused)focusedCount++;}
        }else{
          for(const item of geometry.roomsFor(floorKey)){
            const chosen=focused&&selectedRoom&&item.name.toUpperCase()===selectedRoom.toUpperCase();
            const available=free.has(item.name.toUpperCase());room(layer,[item.x,item.y,38,32],chosen?0xd28c35:available?0x255e47:0xa5b5a9,opacity,item.name.split('GSR ')[1],focused);if(focused)focusedCount++;
            if(chosen)target=new THREE.Vector3((item.x-800)/100+offset.x,(floor-2)*1.55+.4,(item.y-600)/100+offset.z);
          }
        }
      }
      if(showExterior) exterior(building);
    }
    stack=world;
    const focusKey=key+'|'+(selectedRoom||'')+'|'+focusRequest;
    if(focusKey!==lastFocus){fitCamera();if(target)focusRoom(target);lastFocus=focusKey;}
    message.hidden=Boolean(selectedRoom&&target);
    message.textContent=selectedRoom&&!target?'Room location unavailable in the model.':selectedRoom?'Selected room highlighted · approximate campus model.':buildingMessage(selectedBuilding,focusedCount,showOtherFloors);
    render();
  }
  function buildingMessage(building,count,showOtherFloors){return building==='LKS'?`${count} mapped project rooms · availability unknown: LKS is not in the current snapshot.`:`${count} mapped GSRs · Orange: selected room · Green: available · Grey: not shown as available. Other floors ${showOtherFloors?'translucent':'hidden'}.`;}
  document.getElementById('campus-overview').addEventListener('click',()=>{cancelAnimationFrame(animation);animation=0;fitCamera();render();});
  controls.addEventListener('start',()=>{cancelAnimationFrame(animation);animation=0;});
  window.addEventListener('sagasu:floor',update);new ResizeObserver(resize).observe(host);resize();window.dispatchEvent(new Event('sagasu:floor-ready'));
} catch (_) {message.hidden=false;message.textContent='3D floor view unavailable. Use the floor-plan image and available-room list below.';}
