(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusGarmentRig = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function inverse3(a) {
    const [a0,a1,a2,a3,a4,a5,a6,a7,a8] = a;
    const cof = [a4*a8-a5*a7,a2*a7-a1*a8,a1*a5-a2*a4,
      a5*a6-a3*a8,a0*a8-a2*a6,a2*a3-a0*a5,
      a3*a7-a4*a6,a1*a6-a0*a7,a0*a4-a1*a3];
    const det = a0*cof[0]+a1*cof[3]+a2*cof[6];
    if (Math.abs(det) < 1e-12) throw new Error("Degenerate garment binding");
    return cof.map(v => v/det);
  }

  function binding(reference) {
    const rows = [];
    const normal = Array(9).fill(0);
    for (let n=0;n<reference.length;n+=2) {
      const row=[reference[n],reference[n+1],1];
      rows.push(row);
      for(let i=0;i<3;i++)for(let j=0;j<3;j++)normal[i*3+j]+=row[i]*row[j];
    }
    const inv=inverse3(normal);
    const weights=rows.map(row=>[0,1,2].map(i=>inv[i*3]*row[0]+inv[i*3+1]*row[1]+inv[i*3+2]));
    return current=>{
      const matrix=[0,0,0,0,0,0];
      weights.forEach((w,n)=>{for(let i=0;i<3;i++){matrix[i]+=w[i]*current[n*2];matrix[i+3]+=w[i]*current[n*2+1];}});
      return matrix;
    };
  }

  const cross=(a,b,p)=>(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);
  const area=polygon=>polygon.reduce((sum,p,i)=>{
    const q=polygon[(i+1)%polygon.length];return sum+p[0]*q[1]-q[0]*p[1];
  },0)/2;

  function clipHalfPlane(polygon, a, b, inside) {
    const result=[],sign=inside?1:-1;
    for(let i=0;i<polygon.length;i++){
      const p=polygon[i],q=polygon[(i+1)%polygon.length];
      const dp=sign*cross(a,b,p),dq=sign*cross(a,b,q);
      if(dp>=0)result.push(p);
      if((dp>=0)!==(dq>=0)){
        const t=dp/(dp-dq);
        result.push(p.map((value,k)=>value+t*(q[k]-value)));
      }
    }
    return result;
  }

  // Convex cutouts are authored in each component's local UV coordinates.
  // Clip triangles, interpolating UVs and positions together, rather than
  // dropping whole grid cells or changing the shared texture atlas.
  function cutTriangles(vertices, triangles, cutouts) {
    for(const outline of cutouts){
      const cut=area(outline)<0?[...outline].reverse():outline;
      const next=[];
      for(const triangle of triangles){
        const source=triangle.map(i=>vertices[i]);
        let overlap=source;
        for(let e=0;e<cut.length&&overlap.length;e++)overlap=clipHalfPlane(overlap,cut[e],cut[(e+1)%cut.length],true);
        if(Math.abs(area(overlap))<1e-12){next.push(triangle);continue;}
        let remainder=source;
        for(let e=0;e<cut.length&&remainder.length;e++){
          const a=cut[e],b=cut[(e+1)%cut.length];
          const outside=clipHalfPlane(remainder,a,b,false);
          if(Math.abs(area(outside))>1e-12){
            const start=vertices.length;vertices.push(...outside);
            for(let i=1;i<outside.length-1;i++){
              if(Math.abs(cross(outside[0],outside[i],outside[i+1]))>1e-12)next.push([start,start+i,start+i+1]);
            }
          }
          remainder=clipHalfPlane(remainder,a,b,true);
        }
      }
      triangles=next;
    }
    return triangles.flat();
  }

  function grid(component, config) {
    const columns=component.columns||12, rows=component.rows||12;
    const vertices=[], triangles=[];
    const [left,top,width,height]=component.atlas;
    const [a,b,c,d]=component.corners;
    for(let y=0;y<=rows;y++)for(let x=0;x<=columns;x++) {
      const u=x/columns,v=y/rows;
      const px=(1-u)*(1-v)*a[0]+u*(1-v)*b[0]+u*v*c[0]+(1-u)*v*d[0];
      const py=(1-u)*(1-v)*a[1]+u*(1-v)*b[1]+u*v*c[1]+(1-u)*v*d[1];
      vertices.push([u,v,(px-config.origin[0])/config.pixelsPerUnit,(config.origin[1]-py)/config.pixelsPerUnit]);
      if(x<columns&&y<rows){const i=y*(columns+1)+x;triangles.push([i,i+columns+1,i+1],[i+1,i+columns+1,i+columns+2]);}
    }
    const indices=cutTriangles(vertices,triangles,component.cutouts||[]);
    const positions=vertices.flatMap(p=>p.slice(2));
    const uvs=vertices.flatMap(([u,v])=>[(left+u*width)/config.textureSize[0],1-(top+v*height)/config.textureSize[1]]);
    return {positions:new Float32Array(positions),uvs:new Float32Array(uvs),indices:new Uint16Array(indices),localV:vertices.map(p=>p[1])};
  }

  function install(model, config) {
    const core=model.internalModel.coreModel;
    if(core.__amadeusGarmentRig) return core.__amadeusGarmentRig;
    if(!model.textures?.[config.textureIndex])throw new Error("Garment texture was not loaded");
    const raw=core.getModel();
    const bodySpec=config.bodyBinding;
    const bodyIndex=bodySpec?core.getDrawableIndex(bodySpec.id):-1;
    if(bodySpec&&(bodyIndex<0||bodySpec.referenceVertices.length!==raw.drawables.vertexPositions[bodyIndex].length)){
      throw new Error("Body binding does not match model");
    }
    const bodyFit=bodySpec?binding(bodySpec.referenceVertices):null;
    const original={};
    for(const key of ["getDrawableVertexCount","getDrawableVertexIndexCount","getDrawableVertices",
      "getDrawableVertexPositions","getDrawableVertexIndices","getDrawableVertexUvs","getDrawableTextureIndices",
      "getDrawableOpacity","getDrawableDynamicFlagIsVisible","getDrawableCulling","getDrawableRenderOrders"]){
      original[key]=core[key].bind(core);
    }
    const entries=new Map();
    const hidden=new Set((config.hiddenDrawables||[]).map(id=>core.getDrawableIndex(id)));
    for(const spec of config.layers) {
      const index=core.getDrawableIndex(spec.id), bindIndex=core.getDrawableIndex(spec.bindTo||spec.id);
      if(index<0||bindIndex<0)throw new Error("Missing garment drawable: "+spec.id);
      if(spec.atlas&&spec.referenceVertices.length!==raw.drawables.vertexPositions[bindIndex].length){
        throw new Error("Garment binding does not match model: "+spec.id);
      }
      const patch=spec.atlas?grid(spec,config):{positions:new Float32Array(),uvs:new Float32Array(),indices:new Uint16Array()};
      const posePatch=spec.crossedCorners?grid({...spec,corners:spec.crossedCorners},config):null;
      if(posePatch){
        const start=spec.poseStart??0;
        for(let i=0;i<posePatch.positions.length;i+=2){
          const v=patch.localV[i/2];
          const weight=Math.max(0,Math.min(1,(v-start)/(1-start)));
          posePatch.positions[i]=patch.positions[i]+weight*(posePatch.positions[i]-patch.positions[i]);
          posePatch.positions[i+1]=patch.positions[i+1]+weight*(posePatch.positions[i+1]-patch.positions[i+1]);
        }
      }
      // A shirt underlayer can retain pieces from several Core drawables while
      // their former slots draw only the outer jacket. This separates z-order
      // without duplicating any visible shirt surface.
      const sources=spec.baseSources||(spec.retainBase?[spec.id]:[]);
      const baseSegments=[];
      let baseCount=0,baseIndexCount=0;
      for(const id of sources){
        const source=core.getDrawableIndex(id);
        if(source<0)throw new Error("Missing retained garment source: "+id);
        const count=original.getDrawableVertexCount(source),sourceIndices=original.getDrawableVertexIndices(source);
        baseSegments.push({source,count,offset:baseCount,indexOffset:baseIndexCount,indices:sourceIndices});
        baseCount+=count;baseIndexCount+=sourceIndices.length;
      }
      const baseIndices=new Uint16Array(baseIndexCount);
      const positions=new Float32Array(baseCount*2+patch.positions.length);
      const uvs=new Float32Array(positions.length);
      const indices=new Uint16Array(baseIndices.length+patch.indices.length);
      if(baseCount){
        for(const segment of baseSegments){
          const baseUv=original.getDrawableVertexUvs(segment.source);
          for(let k=0;k<baseUv.length;k+=2){uvs[segment.offset*2+k]=baseUv[k]*config.baseTextureScale[0];uvs[segment.offset*2+k+1]=1-(1-baseUv[k+1])*config.baseTextureScale[1];}
          for(let k=0;k<segment.indices.length;k++)baseIndices[segment.indexOffset+k]=segment.indices[k]+segment.offset;
        }
        indices.set(baseIndices);
      }
      uvs.set(patch.uvs,baseCount*2);
      for(let n=0;n<patch.indices.length;n++)indices[baseIndices.length+n]=patch.indices[n]+baseCount;
      if(spec.baseOnTop&&baseCount){
        indices.set(Array.from(patch.indices,n=>n+baseCount),0);
        indices.set(baseIndices,patch.indices.length);
      }
      const toModel=point=>[(point[0]-config.origin[0])/config.pixelsPerUnit,(config.origin[1]-point[1])/config.pixelsPerUnit];
      const attachment=spec.baseAttachment?{
        scale:spec.baseAttachment.scale,
        wrist:toModel(spec.baseAttachment.wrist),
        cuff:toModel(spec.baseAttachment.cuff),
      }:null;
      entries.set(index,{spec,index,bindIndex,baseCount,baseSegments,patch,posePatch,positions,uvs,indices,attachment,fit:spec.atlas?binding(spec.referenceVertices):null});
    }
    function update() {
      const ownMatrices=new Map();
      for(const entry of entries.values())if(entry.fit&&!ownMatrices.has(entry.bindIndex)){
        ownMatrices.set(entry.bindIndex,entry.fit(raw.drawables.vertexPositions[entry.bindIndex]));
      }
      const bodyMatrix=bodyFit?bodyFit(raw.drawables.vertexPositions[bodyIndex]):null;
      const followBody=matrix=>bodyMatrix?matrix.map((v,i)=>v+(bodyMatrix[i]-v)*bodySpec.influence):matrix;
      for(const entry of entries.values()) {
        const {positions,patch,baseCount}=entry;
        for(const segment of entry.baseSegments)positions.set(raw.drawables.vertexPositions[segment.source],segment.offset*2);
        const own=ownMatrices.get(entry.bindIndex)||[1,0,0,0,1,0];
        const [a,b,c,d,e,f]=followBody(own);
        if(baseCount&&bodyMatrix){
          // Transfer retained pieces out of their authored arm motion and into
          // the same chest-led motion as their cuff. Keep local finger/cloth
          // deformations; simply adding a body offset would detach the wrist.
          for(const segment of entry.baseSegments){
            const source=ownMatrices.get(segment.source);
            if(!source)throw new Error("Missing retained-piece motion binding");
            const [sa,sb,sc,sd,se,sf]=source,det=sa*se-sb*sd;
            const [ma,mb,mc,md,me,mf]=followBody(source);
            for(let i=segment.offset*2;i<(segment.offset+segment.count)*2;i+=2){
              const px=positions[i]-sc,py=positions[i+1]-sf;
              let x=(se*px-sb*py)/det,y=(-sd*px+sa*py)/det;
              if(entry.attachment){
                const {scale,wrist,cuff}=entry.attachment;
                x=cuff[0]+scale*(x-wrist[0]);y=cuff[1]+scale*(y-wrist[1]);
              }
              positions[i]=ma*x+mb*y+mc;positions[i+1]=md*x+me*y+mf;
            }
          }
        }else if(baseCount&&entry.attachment){
          // Scale the original hand around its moving wrist, then seat that
          // wrist inside the cuff. Both anchors use the sleeve's current fit.
          // Start from Core positions on every update, avoiding cumulative scale.
          const {scale,wrist,cuff}=entry.attachment;
          const wx=a*wrist[0]+b*wrist[1]+c,wy=d*wrist[0]+e*wrist[1]+f;
          const cx=a*cuff[0]+b*cuff[1]+c,cy=d*cuff[0]+e*cuff[1]+f;
          for(let i=0;i<baseCount*2;i+=2){
            positions[i]=cx+scale*(positions[i]-wx);
            positions[i+1]=cy+scale*(positions[i+1]-wy);
          }
        }
        const pose=entry.posePatch?Math.max(0,Math.min(1,core.getParameterValueById(entry.spec.poseParameter))):0;
        for(let i=0;i<patch.positions.length;i+=2){
          const x=patch.positions[i]+(entry.posePatch?pose*(entry.posePatch.positions[i]-patch.positions[i]):0);
          const y=patch.positions[i+1]+(entry.posePatch?pose*(entry.posePatch.positions[i+1]-patch.positions[i+1]):0);
          positions[baseCount*2+i]=a*x+b*y+c;
          positions[baseCount*2+i+1]=d*x+e*y+f;
        }
      }
    }
    const field=(name,key,measure)=>{core[name]=i=>{const e=entries.get(i);return e?(measure?measure(e[key]):e[key]):original[name](i);};};
    field("getDrawableVertices","positions");
    field("getDrawableVertexPositions","positions");
    field("getDrawableVertexUvs","uvs");
    field("getDrawableVertexIndices","indices");
    field("getDrawableVertexCount","positions",a=>a.length/2);
    field("getDrawableVertexIndexCount","indices",a=>a.length);
    core.getDrawableTextureIndices=i=>entries.has(i)?config.textureIndex:original.getDrawableTextureIndices(i);
    core.getDrawableCulling=i=>entries.has(i)?false:original.getDrawableCulling(i);
    core.getDrawableOpacity=i=>hidden.has(i)?0:entries.get(i)?.spec.alwaysVisible?1:original.getDrawableOpacity(i);
    core.getDrawableDynamicFlagIsVisible=i=>hidden.has(i)?false:entries.get(i)?.spec.alwaysVisible?true:original.getDrawableDynamicFlagIsVisible(i);
    const orders=new Int32Array(raw.drawables.count);
    core.getDrawableRenderOrders=()=>{
      const existing=original.getDrawableRenderOrders();
      const back=core.getDrawableIndex(config.backPanel);
      const sequence=Array.from(existing,(_,i)=>i).filter(i=>i!==back).sort((a,b)=>existing[a]-existing[b]);
      const behind=core.getDrawableIndex(config.behindDrawable);
      sequence.splice(Math.max(0,sequence.indexOf(behind)),0,back);
      const front=(config.frontLayers||[]).map(id=>core.getDrawableIndex(id));
      for(const index of front)sequence.splice(sequence.indexOf(index),1);
      sequence.splice(sequence.indexOf(core.getDrawableIndex(config.inFrontOf))+1,0,...front);
      const under=(config.baseLayers||[]).map(id=>core.getDrawableIndex(id));
      for(const index of under)sequence.splice(sequence.indexOf(index),1);
      if(under.length)sequence.splice(sequence.indexOf(core.getDrawableIndex(config.baseLayerTarget)),0,...under);
      sequence.forEach((index,rank)=>orders[index]=rank);
      return orders;
    };
    const adapter={update,entries,dispose(){for(const [name,fn] of Object.entries(original))core[name]=fn;delete core.__amadeusGarmentRig;}};
    core.__amadeusGarmentRig=adapter;
    update();
    return adapter;
  }
  return {install};
});
