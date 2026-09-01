import * as THREE from 'three';
import { OrbitControls }    from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader }       from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader }      from 'three/addons/loaders/DRACOLoader.js';
import { RoomEnvironment }  from 'three/addons/environments/RoomEnvironment.js';

const DRACO_CDN = 'https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/libs/draco/';

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.model = null;
    this.skeletonHelper = null;
    this.turntable = true;
    this.wireframe = false;
    this.bones = false;
    this.joints = false;

    this.boneList = [];        // flat, in traversal order
    this.markers = [];         // one marker mesh per bone, index-aligned with boneList
    this.selectedBone = null;
    this.onBoneSelect = null;  // set by the app to sync the rig panel

    this.renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: false, preserveDrawingBuffer: true,
    });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0a0a09);

    this.camera = new THREE.PerspectiveCamera(38, 1, 0.01, 500);
    this.camera.position.set(2.4, 1.7, 2.4);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.minDistance = 0.2;
    this.controls.maxDistance = 60;

    // Image-based lighting does most of the work; the key light adds a shadow.
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.envMap = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environment = this.envMap;

    this.key = new THREE.DirectionalLight(0xffffff, 2.1);
    this.key.position.set(3, 5, 2.5);
    this.key.castShadow = true;
    this.key.shadow.mapSize.set(2048, 2048);
    this.key.shadow.bias = -0.0012;
    this.key.shadow.normalBias = 0.02;
    const c = this.key.shadow.camera;
    c.near = 0.1; c.far = 30; c.left = c.bottom = -4; c.right = c.top = 4;
    this.scene.add(this.key);
    this.scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x2a2620, 0.35));

    this.ground = new THREE.Mesh(
      new THREE.PlaneGeometry(60, 60),
      new THREE.ShadowMaterial({ opacity: 0.34 }),
    );
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.receiveShadow = true;
    this.scene.add(this.ground);

    this.grid = new THREE.GridHelper(20, 40, 0x3a3833, 0x232120);
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.5;
    this.scene.add(this.grid);

    // Joint markers live in world space and are re-positioned every frame, so
    // they track the turntable and any future posing without reparenting.
    this.jointGroup = new THREE.Group();
    this.jointGroup.visible = false;
    this.scene.add(this.jointGroup);

    this.jointGeo = new THREE.SphereGeometry(1, 12, 8);
    this.jointMat = new THREE.MeshBasicMaterial({ color: 0x8fb7ff, depthTest: false, transparent: true, opacity: 0.85 });
    this.jointMatSel = new THREE.MeshBasicMaterial({ color: 0xe8654a, depthTest: false });

    const draco = new DRACOLoader().setDecoderPath(DRACO_CDN);
    this.loader = new GLTFLoader().setDRACOLoader(draco);

    this._raycaster = new THREE.Raycaster();
    this._pointer = new THREE.Vector2();
    this._installPicking();

    this._resize();
    new ResizeObserver(() => this._resize()).observe(canvas.parentElement);

    this.clock = new THREE.Clock();
    this.renderer.setAnimationLoop(() => this._tick());
  }

  /* ── picking ───────────────────────────────────────────────────────────── */

  _installPicking() {
    let downAt = null;
    this.canvas.addEventListener('pointerdown', (e) => { downAt = { x: e.clientX, y: e.clientY }; });
    this.canvas.addEventListener('pointerup', (e) => {
      if (!downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      downAt = null;
      if (moved > 4) return;                       // that was an orbit drag, not a click
      if (!this.joints || !this.markers.length) return;

      const rect = this.canvas.getBoundingClientRect();
      this._pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      this._pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      this._raycaster.setFromCamera(this._pointer, this.camera);
      const hit = this._raycaster.intersectObjects(this.markers, false)[0];
      if (!hit) return;
      const bone = hit.object.userData.bone;
      this.selectBone(bone.uuid);
      this.onBoneSelect?.(bone.uuid);
    });
  }

  _resize() {
    const el = this.canvas.parentElement;
    const w = el.clientWidth || 1;
    const h = el.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  _tick() {
    const dt = this.clock.getDelta();
    if (this.turntable && this.model) this.model.rotation.y += dt * 0.36;
    this.controls.update();
    if (this.joints) this._syncMarkers();
    this.renderer.render(this.scene, this.camera);
  }

  /** Markers sit on the bone's world pivot and keep a near-constant screen size. */
  _syncMarkers() {
    const camPos = this.camera.position;
    for (const m of this.markers) {
      m.userData.bone.getWorldPosition(m.position);
      const d = camPos.distanceTo(m.position);
      const r = d * 0.006 * (m === this.selectedMarker ? 1.7 : 1);
      m.scale.setScalar(r);
    }
  }

  /* ── content ───────────────────────────────────────────────────────────── */

  clear() {
    if (this.skeletonHelper) {
      this.scene.remove(this.skeletonHelper);
      this.skeletonHelper.geometry?.dispose();
      this.skeletonHelper.material?.dispose();
      this.skeletonHelper = null;
    }
    this.jointGroup.clear();
    this.markers = [];
    this.boneList = [];
    this.selectedBone = null;
    this.selectedMarker = null;
    if (!this.model) return;
    this.scene.remove(this.model);
    this.model.traverse((o) => {
      if (!o.isMesh) return;
      o.geometry?.dispose();
      for (const m of [].concat(o.material || [])) {
        for (const k of Object.keys(m)) if (m[k]?.isTexture) m[k].dispose();
        m.dispose?.();
      }
    });
    this.model = null;
  }

  async load(url) {
    this.clear();
    const gltf = await this.loader.loadAsync(url);
    const root = gltf.scene;

    let verts = 0, tris = 0, meshes = 0, bones = 0, skinnedMeshes = 0, morphTargets = 0;
    const mats = new Set();
    root.traverse((o) => {
      if (o.isBone) { bones += 1; this.boneList.push(o); }
      if (!o.isMesh) return;
      meshes += 1;
      if (o.isSkinnedMesh) skinnedMeshes += 1;
      o.castShadow = o.receiveShadow = true;
      const g = o.geometry;
      verts += g.attributes.position?.count ?? 0;
      tris  += (g.index ? g.index.count : g.attributes.position?.count ?? 0) / 3;
      morphTargets += Object.values(o.morphTargetDictionary || {}).length;
      for (const m of [].concat(o.material || [])) {
        mats.add(m.uuid);
        m.envMapIntensity = 1.0;
        if (m.map) m.map.colorSpace = THREE.SRGBColorSpace;
      }
    });

    // Normalise to a ~1.6 unit tall object sitting on the grid, so every
    // asset is judged at the same apparent scale regardless of export units.
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const scale = 1.6 / (Math.max(size.x, size.y, size.z) || 1);

    const pivot = new THREE.Group();
    root.position.sub(centre);
    root.position.y += size.y / 2;
    pivot.add(root);
    pivot.scale.setScalar(scale);
    this.scene.add(pivot);
    this.model = pivot;

    if (bones) {
      this.skeletonHelper = new THREE.SkeletonHelper(root);
      this.skeletonHelper.visible = this.bones;
      this.skeletonHelper.material.depthTest = false;
      this.skeletonHelper.material.transparent = true;
      this.skeletonHelper.material.opacity = 0.9;
      this.scene.add(this.skeletonHelper);

      for (const bone of this.boneList) {
        const m = new THREE.Mesh(this.jointGeo, this.jointMat);
        m.renderOrder = 999;
        m.userData.bone = bone;
        this.jointGroup.add(m);
        this.markers.push(m);
      }
      this._syncMarkers();
    }

    this.setWireframe(this.wireframe);
    this.frame();

    return {
      vertices: verts,
      triangles: Math.round(tris),
      materials: mats.size,
      meshes,
      bones,
      skinnedMeshes,
      morphTargets,
      animations: gltf.animations?.length || 0,
      dimensions: size.toArray().map((v) => +v.toFixed(3)),
    };
  }

  /* ── skeleton ──────────────────────────────────────────────────────────── */

  /** Nested {uuid, name, depth, children} for the rig panel's tree. */
  boneTree() {
    if (!this.boneList.length) return [];
    const set = new Set(this.boneList);
    const node = (bone, depth) => ({
      uuid: bone.uuid,
      name: bone.name || '(unnamed)',
      depth,
      children: bone.children.filter((c) => set.has(c)).map((c) => node(c, depth + 1)),
    });
    return this.boneList.filter((b) => !set.has(b.parent)).map((b) => node(b, 0));
  }

  boneStats(uuid) {
    const bone = this.boneList.find((b) => b.uuid === uuid);
    if (!bone) return null;
    const world = bone.getWorldPosition(new THREE.Vector3());
    const children = bone.children.filter((c) => c.isBone);
    const len = children.length
      ? children.reduce((acc, c) => acc + c.getWorldPosition(new THREE.Vector3()).distanceTo(world), 0) / children.length
      : 0;
    return {
      name: bone.name || '(unnamed)',
      parent: bone.parent?.isBone ? bone.parent.name : 'root',
      children: children.length,
      length: +(len / (this.model?.scale.x || 1)).toFixed(4),
      position: bone.position.toArray().map((v) => +v.toFixed(4)),
      rotation: [bone.rotation.x, bone.rotation.y, bone.rotation.z].map((v) => +(v * 180 / Math.PI).toFixed(2)),
    };
  }

  selectBone(uuid) {
    this.selectedBone = uuid;
    this.selectedMarker = null;
    for (const m of this.markers) {
      const on = m.userData.bone.uuid === uuid;
      m.material = on ? this.jointMatSel : this.jointMat;
      if (on) this.selectedMarker = m;
    }
    // Selecting a joint is meaningless if you cannot see the rig, so show it.
    if (uuid && !this.joints) this.setJoints(true);
    this._syncMarkers();
  }

  focusBone(uuid) {
    const bone = this.boneList.find((b) => b.uuid === uuid);
    if (!bone) return;
    const target = bone.getWorldPosition(new THREE.Vector3());
    const offset = this.camera.position.clone().sub(this.controls.target);
    const dist = Math.max(0.35, offset.length() * 0.55);
    this.controls.target.copy(target);
    this.camera.position.copy(target).add(offset.setLength(dist));
    this.controls.update();
  }

  frame() {
    if (!this.model) return;
    const box = new THREE.Box3().setFromObject(this.model);
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const dist = sphere.radius / Math.sin((this.camera.fov * Math.PI) / 360) * 1.25;
    const dir = new THREE.Vector3(0.72, 0.46, 0.98).normalize();
    this.camera.position.copy(sphere.center).addScaledVector(dir, dist);
    this.controls.target.copy(sphere.center);
    this.controls.update();
  }

  setWireframe(on) {
    this.wireframe = on;
    this.model?.traverse((o) => {
      if (!o.isMesh) return;
      for (const m of [].concat(o.material || [])) m.wireframe = on;
    });
  }

  setBones(on) {
    this.bones = on;
    if (this.skeletonHelper) this.skeletonHelper.visible = on;
  }

  setJoints(on) {
    this.joints = on;
    this.jointGroup.visible = on && this.markers.length > 0;
    if (on) this._syncMarkers();
  }

  setGrid(on)  { this.grid.visible = on; this.ground.visible = on; }
  setEnv(on)   { this.scene.environment = on ? this.envMap : null; this.key.intensity = on ? 2.1 : 3.4; }
  setTurntable(on) { this.turntable = on; }

  /** Square thumbnail, rendered from a fixed three-quarter angle. */
  screenshot(size = 512) {
    if (!this.model) return null;
    const prevRot = this.model.rotation.y;
    const helperVisible = this.skeletonHelper?.visible;
    const jointsVisible = this.jointGroup.visible;
    const { width, height } = this.renderer.domElement;
    const prevAspect = this.camera.aspect;

    this.model.rotation.y = -0.55;
    if (this.skeletonHelper) this.skeletonHelper.visible = false;
    this.jointGroup.visible = false;
    this.renderer.setSize(size, size, false);
    this.camera.aspect = 1;
    this.camera.updateProjectionMatrix();
    this.frame();
    this.renderer.render(this.scene, this.camera);
    const url = this.renderer.domElement.toDataURL('image/png');

    this.model.rotation.y = prevRot;
    if (this.skeletonHelper) this.skeletonHelper.visible = helperVisible;
    this.jointGroup.visible = jointsVisible;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = prevAspect;
    this.camera.updateProjectionMatrix();
    this.frame();
    return url;
  }
}
