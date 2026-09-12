import { useEffect, useRef } from "react";
import * as THREE from "three";
import { useAiActivity } from "@/hooks/useAiActivity";
import { fx } from "@/lib/pointerFx";

interface Props {
  density?: number;
  glow?: boolean;
}

/**
 * Reactive 3D neural-constellation background — a faithful port of the design
 * prototype's `initThree`. Violet nodes joined by dim links, rotating slowly and
 * parallaxing toward the cursor. Nodes/links near the cursor light up, clicks
 * send a ring rippling through the mesh (via lib/pointerFx), cyan energy packets
 * travel the links, the field "breathes" idly (plus a slower ~25s world
 * breathing cycle and an idle/awake state that calms after 8s without mouse
 * movement), and everything intensifies while the Claude CLI is working. A far
 * multi-colour dust cloud parallaxes more strongly than the network, temporary
 * light-blue constellations form and fade every ~8-16s, and a rare (~60-150s)
 * synchronize wave sweeps across the graph. Purely decorative (pointer-events:
 * none).
 */
export function NeuralBackground({ density = 140, glow = true }: Props) {
  const mount = useRef<HTMLDivElement>(null);
  const { data } = useAiActivity();
  const aiActive = (data?.running?.length ?? 0) > 0;

  const targetEnergy = useRef(0);
  useEffect(() => {
    targetEnergy.current = aiActive ? 1 : 0;
  }, [aiActive]);

  useEffect(() => {
    const el = mount.current;
    if (!el) return;
    const reduce =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const W = () => el.clientWidth || window.innerWidth;
    const H = () => el.clientHeight || window.innerHeight;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, W() / H(), 1, 4000);
    camera.position.z = 720;
    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(W(), H());
    el.appendChild(renderer.domElement);

    const N = Math.max(30, Math.min(180, density));
    const pts: THREE.Vector3[] = [];
    const pos: number[] = [];
    const col: number[] = [];
    const base: number[] = [];
    const baseCol = new THREE.Color(0xa78bfa);
    for (let i = 0; i < N; i++) {
      const v = new THREE.Vector3(
        (Math.random() - 0.5) * 1500,
        (Math.random() - 0.5) * 950,
        (Math.random() - 0.5) * 720,
      );
      pts.push(v);
      pos.push(v.x, v.y, v.z);
      col.push(baseCol.r, baseCol.g, baseCol.b);
      base.push(0.55 + Math.random() * 0.25);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(col.slice(), 3));
    const points = new THREE.Points(
      g,
      new THREE.PointsMaterial({
        size: 6,
        transparent: true,
        opacity: 0.92,
        vertexColors: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    );

    const edges: Array<[number, number]> = [];
    const lp: number[] = [];
    const lc: number[] = [];
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        if (pts[i].distanceTo(pts[j]) < 240) {
          edges.push([i, j]);
          lp.push(pts[i].x, pts[i].y, pts[i].z, pts[j].x, pts[j].y, pts[j].z);
          lc.push(0, 0, 0, 0, 0, 0);
        }
      }
    }
    const lg = new THREE.BufferGeometry();
    lg.setAttribute("position", new THREE.Float32BufferAttribute(lp, 3));
    lg.setAttribute("color", new THREE.Float32BufferAttribute(lc, 3));
    const lines = new THREE.LineSegments(
      lg,
      new THREE.LineBasicMaterial({
        transparent: true,
        opacity: 0.5,
        vertexColors: true,
        blending: THREE.AdditiveBlending,
      }),
    );
    const lineBase = 0.16;
    const lineCol = new THREE.Color(0x6d5cff);

    // Cyan energy packets travelling along the links.
    const PMAX = reduce ? 8 : 22;
    const ppos = new Float32Array(PMAX * 3);
    const pcolArr = new Float32Array(PMAX * 3);
    const pg = new THREE.BufferGeometry();
    pg.setAttribute("position", new THREE.BufferAttribute(ppos, 3));
    pg.setAttribute("color", new THREE.BufferAttribute(pcolArr, 3));
    const packets = new THREE.Points(
      pg,
      new THREE.PointsMaterial({
        size: 9,
        transparent: true,
        opacity: 0.95,
        vertexColors: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    );
    const pk: Array<{ e: [number, number]; t: number; spd: number }> = [];
    const packColor = new THREE.Color(0x67e8f9);
    const spawnPacket = () => {
      if (!edges.length || pk.length >= PMAX) return;
      const e = edges[(Math.random() * edges.length) | 0];
      pk.push({ e, t: Math.random() * 0.2, spd: 0.006 + Math.random() * 0.01 });
    };

    const group = new THREE.Group();
    group.add(lines);
    group.add(points);
    group.add(packets);
    scene.add(group);

    // Far dust cloud — thousands of tiny multi-colour particles, a parallax
    // layer that reacts more strongly to the mouse than the network.
    const DUST = reduce ? 900 : 1500;
    const dpos = new Float32Array(DUST * 3);
    const dcol = new Float32Array(DUST * 3);
    const dphase = new Float32Array(DUST);
    const dhue: THREE.Color[] = [];
    const dustCols = [
      new THREE.Color(0x8b5cf6),
      new THREE.Color(0x22d3ee),
      new THREE.Color(0x6366f1),
    ];
    for (let i = 0; i < DUST; i++) {
      dpos[i * 3] = (Math.random() - 0.5) * 2600;
      dpos[i * 3 + 1] = (Math.random() - 0.5) * 1700;
      dpos[i * 3 + 2] = (Math.random() - 0.5) * 1400;
      dphase[i] = Math.random() * Math.PI * 2;
      dhue.push(dustCols[(Math.random() * dustCols.length) | 0]);
    }
    const dg = new THREE.BufferGeometry();
    dg.setAttribute("position", new THREE.BufferAttribute(dpos, 3));
    dg.setAttribute("color", new THREE.BufferAttribute(dcol, 3));
    const dust = new THREE.Points(
      dg,
      new THREE.PointsMaterial({
        size: 4.6,
        transparent: true,
        opacity: 0.8,
        vertexColors: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        sizeAttenuation: true,
      }),
    );
    const dustGroup = new THREE.Group();
    dustGroup.add(dust);
    scene.add(dustGroup);
    const dColAttr = dg.getAttribute("color") as THREE.BufferAttribute;

    // Constellations — temporary geometric formations that fade in/out.
    const CSEG = 28;
    const cpos = new Float32Array(CSEG * 6);
    const ccol = new Float32Array(CSEG * 6);
    const cgeo = new THREE.BufferGeometry();
    cgeo.setAttribute("position", new THREE.BufferAttribute(cpos, 3));
    cgeo.setAttribute("color", new THREE.BufferAttribute(ccol, 3));
    const consLines = new THREE.LineSegments(
      cgeo,
      new THREE.LineBasicMaterial({
        transparent: true,
        opacity: 0.7,
        vertexColors: true,
        blending: THREE.AdditiveBlending,
      }),
    );
    group.add(consLines);
    let cons: { segs: Array<[number, number]>; t: number; life: number } | null = null;
    const consColor = new THREE.Color(0x93c5fd);
    const formConstellation = () => {
      const anchor = (Math.random() * N) | 0;
      const near: Array<[number, number]> = [];
      for (let i = 0; i < N; i++) {
        if (i === anchor) continue;
        near.push([i, pts[anchor].distanceTo(pts[i])]);
      }
      near.sort((a, b) => a[1] - b[1]);
      const pick = near.slice(0, 3 + ((Math.random() * 3) | 0)).map((x) => x[0]);
      const segs: Array<[number, number]> = [];
      pick.forEach((p) => segs.push([anchor, p]));
      for (let a = 0; a < pick.length; a++) {
        for (let b = a + 1; b < pick.length; b++) {
          if (Math.random() < 0.5) segs.push([pick[a], pick[b]]);
        }
      }
      cons = { segs: segs.slice(0, CSEG), t: 0, life: 5.5 };
    };

    let mx = 0;
    let my = 0;
    let haveMouse = false;
    let lastActivity = performance.now();
    const onMove = (e: MouseEvent) => {
      mx = e.clientX / window.innerWidth - 0.5;
      my = e.clientY / window.innerHeight - 0.5;
      haveMouse = true;
      lastActivity = performance.now();
    };
    const onResize = () => {
      camera.aspect = W() / H();
      camera.updateProjectionMatrix();
      renderer.setSize(W(), H());
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("resize", onResize);

    const clock = new THREE.Clock();
    const tmp = new THREE.Vector3();
    const colAttr = g.getAttribute("color") as THREE.BufferAttribute;
    const lColAttr = lg.getAttribute("color") as THREE.BufferAttribute;
    const cPosAttr = cgeo.getAttribute("position") as THREE.BufferAttribute;
    const cColAttr = cgeo.getAttribute("color") as THREE.BufferAttribute;
    const act = new Float32Array(N);
    let energy = 0;
    let awake = 1;
    let seenEpoch = fx.epoch;
    let raf = 0;

    // Rare (~60-150s) synchronize wave sweeping the graph.
    let ev: { t: number } | null = null;
    let nextEvent = performance.now() + (60 + Math.random() * 60) * 1000;
    // Constellations forming every ~8-16s.
    let nextCons = performance.now() + (7 + Math.random() * 7) * 1000;

    const animate = () => {
      raf = requestAnimationFrame(animate);
      if (document.hidden) return;
      const t = clock.getElapsedTime();
      const now = performance.now();
      energy += (targetEnergy.current - energy) * 0.05;

      // A fresh click ripple spawns a short burst of packets and counts as activity.
      if (fx.epoch !== seenEpoch) {
        for (let k = 0; k < 3; k++) spawnPacket();
        seenEpoch = fx.epoch;
        lastActivity = now;
      }

      // Idle intelligence — calmer after 8s of no mouse movement, wakes on move.
      const idleFor = (now - lastActivity) / 1000;
      const wakeTgt = idleFor > 8 ? 0.35 : 1;
      awake += (wakeTgt - awake) * 0.02;
      // World breathing (~25s inhale/exhale) layered under the faster breathe cycle.
      const world = 0.5 + 0.5 * Math.sin(t * 0.25);
      const breathe = 0.5 + 0.5 * Math.sin(t * 0.6 * awake);

      const rot = 0.02 + 0.015 * awake + energy * 0.05;
      group.rotation.y = t * rot + mx * 0.45;
      group.rotation.x = my * 0.3 + Math.sin(t * 0.1) * 0.05;
      // Dust parallax — reacts more strongly to the mouse than the network.
      dustGroup.rotation.y = t * 0.012 * awake - mx * 0.28;
      dustGroup.rotation.x = Math.sin(t * 0.07) * 0.03 - my * 0.18;
      dustGroup.position.x += (-mx * 90 - dustGroup.position.x) * 0.04;
      dustGroup.position.y += (my * 60 - dustGroup.position.y) * 0.04;
      camera.position.x += (mx * 140 - camera.position.x) * 0.03;
      camera.position.y += (-my * 90 - camera.position.y) * 0.03;
      camera.lookAt(scene.position);
      group.updateMatrixWorld();

      // Environmental event + constellation scheduling.
      if (now > nextEvent && !ev) {
        ev = { t: 0 };
        nextEvent = now + (70 + Math.random() * 80) * 1000;
      }
      if (ev) {
        ev.t += 0.006;
        if (ev.t >= 1) ev = null;
      }
      if (now > nextCons) {
        formConstellation();
        nextCons = now + (8 + Math.random() * 8) * 1000;
      }

      // Dust twinkle + breathing density.
      const dustPulse = 0.45 + 0.55 * world;
      const twSpd = 0.6 + 0.6 * awake;
      for (let i = 0; i < DUST; i++) {
        const tw = 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(t * twSpd + dphase[i]));
        const b = tw * dustPulse * (0.5 + 0.5 * awake);
        const c = dhue[i];
        dColAttr.array[i * 3] = c.r * b;
        dColAttr.array[i * 3 + 1] = c.g * b;
        dColAttr.array[i * 3 + 2] = c.b * b;
      }
      dColAttr.needsUpdate = true;
      dust.material.opacity = (0.5 + 0.35 * world) * (0.6 + 0.4 * awake);

      const ripples = fx.ripples;
      for (let i = 0; i < N; i++) {
        tmp.copy(pts[i]).applyMatrix4(group.matrixWorld).project(camera);
        let a = 0;
        if (haveMouse) {
          const dx = tmp.x - mx * 2;
          const dy = tmp.y + my * 2;
          const d = Math.sqrt(dx * dx + dy * dy);
          a = Math.max(0, 1 - d / 0.42);
          a *= a;
          a *= awake;
        }
        for (let r = 0; r < ripples.length; r++) {
          const rp = ripples[r];
          const dx = tmp.x - rp.x;
          const dy = tmp.y - rp.y;
          const d = Math.sqrt(dx * dx + dy * dy);
          const ring = Math.max(0, 1 - Math.abs(d - rp.t * 2.2) / 0.18);
          a = Math.max(a, ring * (1 - rp.t));
        }
        if (ev) {
          const sweep = ev.t * 3 - 1.5;
          const band = Math.max(0, 1 - Math.abs(tmp.x - sweep) / 0.28);
          a = Math.max(a, band * Math.sin(ev.t * Math.PI));
        }
        act[i] += (a - act[i]) * 0.2;
        const glowN = Math.min(
          1,
          base[i] * (0.5 + 0.18 * world + 0.2 * breathe + energy * 0.25) + act[i] * 0.9,
        );
        colAttr.array[i * 3] = Math.min(1, baseCol.r * glowN + act[i] * 0.25);
        colAttr.array[i * 3 + 1] = Math.min(1, baseCol.g * glowN + act[i] * 0.5);
        colAttr.array[i * 3 + 2] = Math.min(1, baseCol.b * glowN + act[i] * 0.35);
      }
      colAttr.needsUpdate = true;
      points.material.size = 6 + energy * 2.5 + breathe * 0.6;

      for (let k = 0; k < edges.length; k++) {
        const [i, j] = edges[k];
        const em = (act[i] + act[j]) * 0.5;
        const b = lineBase * (0.5 + 0.3 * world + 0.35 * breathe + energy * 0.5) + em * 0.7;
        const bi = k * 6;
        lColAttr.array[bi] = lineCol.r * b;
        lColAttr.array[bi + 1] = lineCol.g * b;
        lColAttr.array[bi + 2] = lineCol.b * b;
        lColAttr.array[bi + 3] = lineCol.r * b;
        lColAttr.array[bi + 4] = lineCol.g * b;
        lColAttr.array[bi + 5] = lineCol.b * b;
      }
      lColAttr.needsUpdate = true;

      // Constellation render.
      if (cons) {
        cons.t += (0.016 / cons.life) * 3.2;
        if (cons.t >= 1) {
          cons = null;
        } else {
          const fade = Math.sin(cons.t * Math.PI);
          for (let k = 0; k < CSEG; k++) {
            const bi = k * 6;
            if (cons && k < cons.segs.length) {
              const [i, j] = cons.segs[k];
              cPosAttr.array[bi] = pts[i].x;
              cPosAttr.array[bi + 1] = pts[i].y;
              cPosAttr.array[bi + 2] = pts[i].z;
              cPosAttr.array[bi + 3] = pts[j].x;
              cPosAttr.array[bi + 4] = pts[j].y;
              cPosAttr.array[bi + 5] = pts[j].z;
              const b = fade * 0.8;
              cColAttr.array[bi] = consColor.r * b;
              cColAttr.array[bi + 1] = consColor.g * b;
              cColAttr.array[bi + 2] = consColor.b * b;
              cColAttr.array[bi + 3] = consColor.r * b;
              cColAttr.array[bi + 4] = consColor.g * b;
              cColAttr.array[bi + 5] = consColor.b * b;
            } else {
              cPosAttr.array[bi] = cPosAttr.array[bi + 3] = 0;
              cPosAttr.array[bi + 1] = cPosAttr.array[bi + 4] = 0;
              cPosAttr.array[bi + 2] = cPosAttr.array[bi + 5] = 99999;
              cColAttr.array[bi] =
                cColAttr.array[bi + 1] =
                cColAttr.array[bi + 2] =
                cColAttr.array[bi + 3] =
                cColAttr.array[bi + 4] =
                cColAttr.array[bi + 5] =
                  0;
            }
          }
          cPosAttr.needsUpdate = true;
          cColAttr.needsUpdate = true;
        }
      } else {
        for (let k = 0; k < CSEG; k++) {
          const bi = k * 6;
          cColAttr.array[bi] =
            cColAttr.array[bi + 1] =
            cColAttr.array[bi + 2] =
            cColAttr.array[bi + 3] =
            cColAttr.array[bi + 4] =
            cColAttr.array[bi + 5] =
              0;
        }
        cColAttr.needsUpdate = true;
      }

      const want = 2 + Math.round(energy * 12);
      while (pk.length < want && Math.random() < 0.3 * awake) spawnPacket();
      for (let p = pk.length - 1; p >= 0; p--) {
        const pkt = pk[p];
        pkt.t += pkt.spd * (0.6 + energy * 1.2) * awake;
        if (pkt.t >= 1) {
          pk.splice(p, 1);
          continue;
        }
        const [i, j] = pkt.e;
        const f = pkt.t;
        ppos[p * 3] = pts[i].x + (pts[j].x - pts[i].x) * f;
        ppos[p * 3 + 1] = pts[i].y + (pts[j].y - pts[i].y) * f;
        ppos[p * 3 + 2] = pts[i].z + (pts[j].z - pts[i].z) * f;
        const fade = Math.sin(f * Math.PI);
        pcolArr[p * 3] = packColor.r * fade;
        pcolArr[p * 3 + 1] = packColor.g * fade;
        pcolArr[p * 3 + 2] = packColor.b * fade;
      }
      for (let p = pk.length; p < PMAX; p++) {
        ppos[p * 3] = 0;
        ppos[p * 3 + 1] = 0;
        ppos[p * 3 + 2] = 99999;
      }
      (pg.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
      (pg.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;

      for (let r = ripples.length - 1; r >= 0; r--) {
        ripples[r].t += 0.02;
        if (ripples[r].t >= 1) ripples.splice(r, 1);
      }
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("resize", onResize);
      g.dispose();
      lg.dispose();
      pg.dispose();
      dg.dispose();
      cgeo.dispose();
      points.material.dispose();
      lines.material.dispose();
      packets.material.dispose();
      dust.material.dispose();
      consLines.material.dispose();
      renderer.dispose();
      el.innerHTML = "";
    };
  }, [density]);

  return (
    <>
      <div className="fixed inset-0 z-0" style={{ background: "var(--bg)" }} />
      <div ref={mount} className="pointer-events-none fixed inset-0 z-[1]" />
      {glow && (
        <>
          <div
            className="pointer-events-none fixed z-[1] rounded-full"
            style={{
              top: "-15%",
              left: "-8%",
              width: 640,
              height: 640,
              background: "radial-gradient(circle,rgba(139,92,246,.28),transparent 62%)",
              filter: "blur(30px)",
              animation: "glowPulse 9s ease-in-out infinite",
            }}
          />
          <div
            className="pointer-events-none fixed z-[1] rounded-full"
            style={{
              bottom: "-20%",
              right: "-6%",
              width: 720,
              height: 720,
              background: "radial-gradient(circle,rgba(99,102,241,.26),transparent 62%)",
              filter: "blur(30px)",
              animation: "glowPulse 11s ease-in-out infinite 1s",
            }}
          />
        </>
      )}
    </>
  );
}
